from typing import List

def chunk_text(text: str, max_chars=1000, overlap=150):
    text = text.strip()
    if not text:
        return []
    chunks = []
    start = 0
    n = len(text)
    print(text)
    while start < n:
        end = min(start + max_chars, n)
        chunks.append(text[start:end].strip())
        if end == n:
            break
        start = end - overlap if end - overlap > 0 else end
    return chunks

'''def chunk_text(text: str, max_chars: int = 1500, overlap: int = 150) -> List[str]:
    """
    Split text into overlapping chunks without memory leaks.
    
    Args:
        text: Input text to split
        max_chars: Maximum characters per chunk
        overlap: Number of characters to overlap between chunks
        
    Returns:
        List of text chunks
    """
    text = text.strip()
    if not text:
        return []
    
    if max_chars <= overlap:
        raise ValueError("max_chars must be greater than overlap")
    
    chunks = []
    start = 0
    n = len(text)
    
    while start < n:
        end = min(start + max_chars, n)
        chunk = text[start:end].strip()
        if chunk:  # Only add non-empty chunks
            chunks.append(chunk)
        
        # Proper termination condition
        if end == n:
            break
            
        # Safe overlap calculation to prevent infinite loops
        start = end - overlap if end - overlap > start else end
        
    return chunks'''