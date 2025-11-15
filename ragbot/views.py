# chat/views.py — enhanced with streaming support
from django.shortcuts import render, redirect
from django.views.decorators.http import require_http_methods
from django.http import JsonResponse, StreamingHttpResponse
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from asgiref.sync import sync_to_async
import json
import asyncio
from .forms import ChatForm
from .vectorstore import get_store
from openai import AsyncOpenAI

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

# Smart query analysis functions
def analyze_query_complexity(user_msg):
    """Analyze query to determine appropriate response parameters"""
    msg_lower = user_msg.lower()
    word_count = len(user_msg.split())
    
    detail_keywords = ['explain in detail', 'explain thoroughly', 'detailed explanation', 
                      'step by step', 'comprehensive', 'elaborate', 'in depth']
    procedure_keywords = ['how to', 'steps', 'procedure', 'process', 'guide', 'tutorial']
    comparison_keywords = ['compare', 'difference', 'versus', 'vs', 'contrast']
    simple_keywords = ['what is', 'define', 'who is', 'when', 'where']
    
    if any(keyword in msg_lower for keyword in detail_keywords):
        return {'max_tokens': 1200, 'snippet_size': 800, 'k_results': 8, 'response_type': 'detailed'}
    elif any(keyword in msg_lower for keyword in procedure_keywords):
        return {'max_tokens': 800, 'snippet_size': 600, 'k_results': 6, 'response_type': 'procedural'}
    elif any(keyword in msg_lower for keyword in comparison_keywords):
        return {'max_tokens': 700, 'snippet_size': 600, 'k_results': 8, 'response_type': 'comparison'}
    elif any(keyword in msg_lower for keyword in simple_keywords) and word_count < 8:
        return {'max_tokens': 300, 'snippet_size': 400, 'k_results': 4, 'response_type': 'simple'}
    else:
        return {'max_tokens': 500, 'snippet_size': 500, 'k_results': 6, 'response_type': 'standard'}

def smart_truncate(text, max_chars=500):
    """Truncate text at sentence boundaries"""
    if len(text) <= max_chars:
        return text
    
    truncated = text[:max_chars]
    last_period = truncated.rfind('.')
    last_exclamation = truncated.rfind('!')
    last_question = truncated.rfind('?')
    
    last_sentence_end = max(last_period, last_exclamation, last_question)
    
    if last_sentence_end > max_chars * 0.7:
        return truncated[:last_sentence_end + 1]
    else:
        return truncated + "..."

async def get_search_results(user_msg, k, snippet_size):
    """Async wrapper for search"""
    def sync_search():
        store = get_store()
        return store.search(user_msg, k=k) if store.index.ntotal > 0 else []
    
    return await sync_to_async(sync_search)()

def build_context(retrieved, snippet_size, response_type):
    """Build context with smart formatting"""
    context_blocks = []
    seen_sources = set()
    
    for score, meta in retrieved:
        snippet = smart_truncate(meta.get("text", ""), snippet_size)
        source = meta.get("source_name", "Unknown")
        chunk_num = meta.get("chunk", 0)
        
        source_key = f"{source}_{chunk_num}"
        if source_key not in seen_sources:
            seen_sources.add(source_key)
            
            if response_type == 'simple':
                context_blocks.append(f"[{source}]: {snippet}")
            else:
                context_blocks.append(
                    f"=== DOCUMENT EXCERPT ===\n"
                    f"Source: {source} (Chunk {chunk_num})\n"
                    f"Relevance Score: {score:.3f}\n"
                    f"Content:\n{snippet}\n"
                    f"=== END EXCERPT ===\n"
                )
    
    return "\n".join(context_blocks) if context_blocks else "No relevant context found."

@require_http_methods(["GET"])
def chat_view(request):
    """Render the chat interface"""
    convo = request.session.get("convo", [])
    form = ChatForm()
    return render(request, "chat/index.html", {"form": form, "convo": convo})

# Your existing synchronous version (keep as fallback)
@require_http_methods(["POST"])
def send_message(request):
    """Handle AJAX message sending - synchronous version"""
    try:
        data = json.loads(request.body)
        user_msg = data.get('message', '').strip()
        
        if not user_msg:
            return JsonResponse({'error': 'Message cannot be empty'}, status=400)
        
        # Smart query analysis
        #query_params = analyze_query_complexity(user_msg)
        query_params = {'max_tokens': 2000, 'snippet_size': 800, 'k_results': 8, 'response_type': 'detailed'}
        print(f"Query type: {query_params['response_type']}, tokens: {query_params['max_tokens']}")
        
        convo = request.session.get("convo", [])
        convo.append({"role": "user", "content": user_msg})
        
        # Search with smart parameters
        store = get_store()
        retrieved = store.search(user_msg, k=query_params['k_results']) if store.index.ntotal > 0 else []
        
        # Build context
        context_text = build_context(retrieved, query_params['snippet_size'], query_params['response_type'])
        
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

        from openai import OpenAI
        client = OpenAI()
        resp = client.chat.completions.create(
            model=settings.OPENAI_CHAT_MODEL,
            messages=messages,
            temperature=0.1,
            max_tokens=query_params['max_tokens']
        )
        
        answer = resp.choices[0].message.content
        
        convo.append({"role": "assistant", "content": answer})
        request.session["convo"] = convo
        request.session.modified = True
        
        return JsonResponse({
            'success': True,
            'user_message': user_msg,
            'assistant_message': answer,
            'response_type': query_params['response_type']
        })
        
    except Exception as e:
        return JsonResponse({'error': f'An error occurred: {str(e)}'}, status=500)

# NEW: Streaming version
@require_http_methods(["POST"])
@csrf_exempt
def send_message_stream(request):
    """Fixed synchronous streaming version"""
    try:
        data = json.loads(request.body)
        user_msg = data.get('message', '').strip()
        
        if not user_msg:
            return JsonResponse({'error': 'Message cannot be empty'}, status=400)
        
        def generate_response():
            try:
                # Send immediate test
                yield f"data: {json.dumps({'type': 'status', 'message': 'Starting...'})}\n\n"
                
                # Query analysis
                #query_params = analyze_query_complexity(user_msg)
                query_params = {'max_tokens': 2000, 'snippet_size': 2000, 'k_results': 8, 'response_type': 'detailed'}
                yield f"data: {json.dumps({'type': 'status', 'message': f'Analyzing query ({query_params['response_type']})...'})}\n\n"
                
                # Update conversation
                convo = request.session.get("convo", [])
                convo.append({"role": "user", "content": user_msg})
                
                # Search status
                yield f"data: {json.dumps({'type': 'status', 'message': 'Searching documents...'})}\n\n"
                
                # Get search results
                store = get_store()
                retrieved = store.search(user_msg, k=query_params['k_results']) if store.index.ntotal > 0 else []
                
                # Build context
                context_text = build_context(retrieved, query_params['snippet_size'], query_params['response_type'])
                
                # Generation status
                yield f"data: {json.dumps({'type': 'status', 'message': 'Generating response...'})}\n\n"
                
                # Build messages
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
                
                # Clear status
                yield f"data: {json.dumps({'type': 'clear_status'})}\n\n"
                
                # Create OpenAI client and stream
                from openai import OpenAI
                client = OpenAI()
                
                stream = client.chat.completions.create(
                    model=settings.OPENAI_CHAT_MODEL,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=query_params['max_tokens'],
                    stream=True
                )
                
                full_response = ""
                for chunk in stream:
                    if chunk.choices[0].delta.content is not None:
                        content = chunk.choices[0].delta.content
                        full_response += content
                        yield f"data: {json.dumps({'type': 'content', 'content': content})}\n\n"
                
                # Update conversation
                convo.append({"role": "assistant", "content": full_response})
                request.session["convo"] = convo
                request.session.modified = True
                
                # Send completion
                yield f"data: {json.dumps({'type': 'done', 'full_response': full_response})}\n\n"
                
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        
        response = StreamingHttpResponse(
            generate_response(), 
            content_type='text/event-stream'
        )
        # Only include WSGI-compatible headers
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        
        return response
        
    except Exception as e:
        return JsonResponse({'error': f'An error occurred: {str(e)}'}, status=500)

@require_http_methods(["POST"]) 
def reset_chat(request):
    """Reset chat conversation"""
    request.session["convo"] = []
    request.session.modified = True
    
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