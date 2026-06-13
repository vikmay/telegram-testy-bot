import re

with open('bot.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Add image to Question class
content = content.replace('    explanation: str\n\n\n@dataclass\nclass StudentState', '    explanation: str\n    image: Optional[str] = None\n\n\n@dataclass\nclass StudentState')

# Update questions_store.save
old_str = 'self.questions_store.save([{"id": q.id, "topic_id": q.topic_id, "type": q.type, "question": q.question, "options": q.options, "answer": q.answer, "explanation": q.explanation} for q in self.questions])'
new_str = 'self.questions_store.save([{"id": q.id, "topic_id": q.topic_id, "type": q.type, "question": q.question, "options": q.options, "answer": q.answer, "explanation": q.explanation, "image": getattr(q, "image", None)} for q in self.questions])'

content = content.replace(old_str, new_str)

with open('bot.py', 'w', encoding='utf-8') as f:
    f.write(content)
