# chat/views.py — enhanced chat UI + RAG answer with AJAX support
from django.shortcuts import render, redirect
from django.views.decorators.http import require_http_methods
from django.http import JsonResponse
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
import json
from .forms import ChatForm
from .vectorstore import get_store
from openai import OpenAI

SYSTEM_PROMPT = (
    "You are a document assistant that provides accurate answers using ONLY the provided context from Google Drive documents. "
    "Follow these strict rules:\n\n"
    
    "1. CONTENT FIDELITY:\n"
    "- Use the EXACT wording, phrases, and terminology from the documents\n"
    "- Preserve original formatting: tables, lists, steps, procedures\n"
    "- Copy numerical values, dates, and specifications exactly as written\n"
    "- Maintain the original structure and organization of information\n\n"
    
    "2. FORMATTING:\n"
    "- Present tables in markdown table format when source contains tabular data\n"
    "- Use numbered lists (1. 2. 3.) for procedures and sequential steps\n"
    "- Use bullet points (•) for non-sequential items and features\n"
    "- Preserve headings and subheadings from the original documents\n\n"
    
    "3. ACCURACY REQUIREMENTS:\n"
    "- Quote directly from documents rather than paraphrasing\n"
    "- Include specific details like measurements, timeframes, and requirements\n"
    "- Do NOT summarize or simplify complex information\n"
    "- If information spans multiple documents, clearly distinguish sources\n\n"
    
    "4. CITATIONS:\n"
    "- Always cite the source document: (Source: filename.docx)\n"
    "- For multiple sources, list all: (Sources: file1.docx, file2.docx)\n"
    "- Place citations immediately after the relevant information\n\n"
    
    "5. LIMITATIONS:\n"
    "- If the answer requires information not in the provided context, state: 'This information is not available in the provided documents.'\n"
    "- Do not add external knowledge or make assumptions\n"
    "- If context is incomplete, mention what specific information is missing\n\n"
    
    "Your goal is to be a faithful representation of the document content, not to improve or interpret it."
)

client = OpenAI()

@require_http_methods(["GET"])
def chat_view(request):
    """Render the chat interface"""
    convo = request.session.get("convo", [])
    form = ChatForm()
    return render(request, "chat/index.html", {"form": form, "convo": convo})

@require_http_methods(["POST"])
def send_message(request):
    """Handle AJAX message sending"""
    try:
        # Parse JSON data from request body
        data = json.loads(request.body)
        user_msg = data.get('message', '').strip()
        
        if not user_msg:
            return JsonResponse({'error': 'Message cannot be empty'}, status=400)
        
        # Get conversation from session
        convo = request.session.get("convo", [])
        
        # Add user message to conversation
        convo.append({"role": "user", "content": user_msg})
        
        # Retrieve with higher k value to get more context
        store = get_store()
        retrieved = store.search(user_msg, k=10) if store.index.ntotal > 0 else []
        
        # Build more comprehensive context
        context_blocks = []
        seen_sources = set()
        
        for score, meta in retrieved:
            # Use longer snippets to preserve formatting
            snippet = meta.get("text", "")[:500]  # Increased from 1200
            source = meta.get("source_name", "Unknown")
            chunk_num = meta.get("chunk", 0)
            
            # Add source tracking to avoid repetition
            source_key = f"{source}_{chunk_num}"
            if source_key not in seen_sources:
                seen_sources.add(source_key)
                
                # Better formatting for context
                context_blocks.append(
                    f"=== DOCUMENT EXCERPT ===\n"
                    f"Source: {source} (Chunk {chunk_num})\n"
                    f"Relevance Score: {score:.3f}\n"
                    f"Content:\n{snippet}\n"
                    f"=== END EXCERPT ===\n"
                )
        
        context_text = "\n".join(context_blocks) if context_blocks else "No relevant context found in the documents."

        # Enhanced message structure
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user", 
                "content": f"User Question: {user_msg}\n\n"
                           f"Please answer this question using ONLY the document context provided below. "
                           f"Maintain exact formatting, wording, and structure from the original documents.\n\n"
                           f"DOCUMENT CONTEXT:\n{context_text}"
            },
        ]

        # Use higher temperature for more faithful reproduction
        resp = client.chat.completions.create(
            model=settings.OPENAI_CHAT_MODEL,
            messages=messages,
            temperature=0.1,  # Lower temperature for more consistent responses
            max_tokens=500,  # Allow longer responses to preserve detail
        )
        
        answer = resp.choices[0].message.content
        
        # Add assistant response to conversation
        convo.append({"role": "assistant", "content": answer})
        
        # Save updated conversation to session
        request.session["convo"] = convo
        request.session.modified = True
        
        # Return JSON response
        return JsonResponse({
            'success': True,
            'user_message': user_msg,
            'assistant_message': answer
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON data'}, status=400)
    except Exception as e:
        return JsonResponse({'error': f'An error occurred: {str(e)}'}, status=500)

@require_http_methods(["POST"]) 
def reset_chat(request):
    """Reset chat conversation"""
    request.session["convo"] = []
    request.session.modified = True
    
    # Handle both AJAX and regular requests
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'message': 'Chat reset successfully'})
    else:
        return redirect("chat")

@require_http_methods(["POST"]) 
def reindex(request):
    """Shortcut button to remind that indexing is via management command"""
    from django.contrib import messages
    messages.info(request, "Use: python manage.py ingest_gdrive --folder-id <FOLDER_ID>")
    return redirect("chat")