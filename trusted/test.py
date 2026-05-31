from memory_store import _build_memory_record

def rec(content, predicate, obj, memory_type="profile"):
    return _build_memory_record({
        "content": content,
        "memory_type": memory_type,
        "category": "other",
        "sensitivity": "low",
        "subject": "user",
        "predicate": predicate,
        "object": obj,
        "value": True,
        "confidence": 0.8,
        "source": "user",
    }, [0.1, 0.2, 0.3])

goal1 = rec("用户正在找 Agent 实习", "has_goal", "Agent 实习")
goal2 = rec("用户正在练英语", "has_goal", "练英语")

style1 = rec("用户喜欢中文回答", "prefers_response_style", "中文回答", "instruction")
style2 = rec("用户喜欢代码示例", "prefers_response_style", "代码示例", "instruction")

like = rec("用户喜欢咖啡", "likes", "咖啡", "preference")
dislike = rec("用户不喜欢咖啡", "dislikes", "咖啡", "preference")

city1 = rec("用户当前城市是北京", "current_city", "北京")
city2 = rec("用户当前城市是上海", "current_city", "上海")

print("goals conflict?", goal1["conflict_key"] == goal2["conflict_key"])
print("styles conflict?", style1["conflict_key"] == style2["conflict_key"])
print("coffee conflict?", like["conflict_key"] == dislike["conflict_key"])
print("city conflict?", city1["conflict_key"] == city2["conflict_key"])

print(goal1["conflict_key"])
print(goal2["conflict_key"])
print(style1["conflict_key"])
print(style2["conflict_key"])
print(like["conflict_key"])
print(dislike["conflict_key"])
print(city1["conflict_key"])
print(city2["conflict_key"])
