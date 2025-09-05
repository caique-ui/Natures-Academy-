from rest_framework import serializers

class AskSerializer(serializers.Serializer):
    question = serializers.CharField()
    top_k = serializers.IntegerField(default=5)
    mode = serializers.ChoiceField(choices=["local", "lamatic"], default="local")
