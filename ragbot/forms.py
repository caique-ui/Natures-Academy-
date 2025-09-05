from django import forms

class ChatForm(forms.Form):
    message = forms.CharField(
        label="",
        widget=forms.Textarea(attrs={
            "rows": 3,
            "placeholder": "Ask about your Drive docs...",
            "class": "textarea"
        }),
    )