from django.shortcuts import render, redirect
from django.views.decorators.http import require_http_methods
from django.conf import settings
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

@require_http_methods(["GET", "POST"])
def chat_view(request):
    # Initialize session conversation (not persisted to DB)
    convo = request.session.get("convo", [])
    form = ChatForm(request.POST or None)
    answer = None

    if request.method == "POST" and form.is_valid():
        user_msg = form.cleaned_data["message"].strip()
        if user_msg:
            convo.append({"role": "user", "content": user_msg})

            # Retrieve
            store = get_store()
            retrieved = store.search(user_msg, k=5) if store.index.ntotal > 0 else []
            context_blocks = []
            for score, meta in retrieved:
                snippet = meta.get("text", "")[:1200]
                source = meta.get("source_name", "?")
                context_blocks.append(f"[score={score:.3f}] {snippet}\n(Source: {source})")
            context_text = "\n\n".join(context_blocks) if context_blocks else "(no matching context retrieved)"

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
                {"role": "system", "content": f"Context from Drive:\n\n{context_text}"},
            ]

            resp = client.chat.completions.create(
                model=settings.OPENAI_CHAT_MODEL,
                messages=messages,
                temperature=0.2,
            )
            answer = resp.choices[0].message.content
            convo.append({"role": "assistant", "content": answer})

            request.session["convo"] = convo
            request.session.modified = True
            return redirect("chat")

    return render(request, "chat/index.html", {"form": form, "convo": convo, "answer": answer})

@require_http_methods(["POST"]) 
def reset_chat(request):
    request.session["convo"] = []
    request.session.modified = True
    return redirect("chat")

@require_http_methods(["POST"]) 
def reindex(request):
    # Shortcut button to remind that indexing is via management command
    from django.contrib import messages
    messages.info(request, "Use: python manage.py ingest_gdrive --folder-id <FOLDER_ID>")
    return redirect("chat")