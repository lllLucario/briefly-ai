"""错误怎样给用户看：只有明确写给用户的说明才返回前端，其余异常的文字只进日志。

UserInputError 继承 ValueError：现有的 except ValueError 和命令行行为不变；接口层只把它转成 400。
其他 ValueError（程序 bug、第三方库、模型输出解析失败）走统一的 500，文字不返回。
"""

MODEL_TIMEOUT = "模型响应超时，这次没有得到结果。可以稍后重试；如果内容较长，可以缩小范围后再试。"
MODEL_FAILED = "模型调用失败，请稍后重试；如果持续出现，请把追踪号发给维护人员。"


class UserInputError(ValueError):
    """消息本身就是给用户看的中文说明：内容为空、格式不支持、文件损坏、缺少服务端依赖等。"""

    user_facing = True


def is_user_facing(error: BaseException) -> bool:
    """这个异常的文字是否写给用户看（类上标了 user_facing）。"""
    return getattr(type(error), "user_facing", False)


def is_timeout(error: BaseException) -> bool:
    import httpx  # 解析子进程也用这个模块，不在模块级导入网络库
    import openai

    return isinstance(error, (openai.APITimeoutError, httpx.TimeoutException))


def failure_text(error: BaseException, fallback: str) -> str:
    """要保存或显示的失败说明。调用方要另外把原始异常记进日志。"""
    if is_user_facing(error):
        return str(error)
    return MODEL_TIMEOUT if is_timeout(error) else fallback


_FIXED_MESSAGES = {
    "missing": "缺少必填项",
    "json_invalid": "请求内容不是有效的 JSON",
    "string_type": "应为文字",
    "list_type": "应为列表",
    "dict_type": "应为对象",
    "model_attributes_type": "应为对象",
    "literal_error": "取值不在允许范围内",
    "enum": "取值不在允许范围内",
    "int_type": "应为整数",
    "int_parsing": "应为整数",
    "int_parsing_size": "应为整数",
    "int_from_float": "应为整数",
    "float_type": "应为数字",
    "float_parsing": "应为数字",
    "bool_type": "应为 true 或 false",
    "bool_parsing": "应为 true 或 false",
    "date_type": "不是有效的日期",
    "date_parsing": "不是有效的日期",
    "date_from_datetime_parsing": "不是有效的日期",
    "date_from_datetime_inexact": "不是有效的日期",
    "datetime_type": "不是有效的时间",
    "datetime_parsing": "不是有效的时间",
    "datetime_from_date_parsing": "不是有效的时间",
}
# 这些错误的 ctx 只有框架算出的数字界限，不含用户输入
_BOUND_MESSAGES = {
    "string_too_long": ("max_length", "不能超过 {} 字"),
    "string_too_short": ("min_length", "不能少于 {} 字"),
    "too_long": ("max_length", "最多 {} 项"),
    "too_short": ("min_length", "至少 {} 项"),
    "greater_than": ("gt", "应大于 {}"),
    "greater_than_equal": ("ge", "不能小于 {}"),
    "less_than": ("lt", "应小于 {}"),
    "less_than_equal": ("le", "不能大于 {}"),
}


def _validation_message(error: dict) -> str:
    if error["type"] in _FIXED_MESSAGES:
        return _FIXED_MESSAGES[error["type"]]
    if error["type"] in _BOUND_MESSAGES:
        key, template = _BOUND_MESSAGES[error["type"]]
        return template.format(error.get("ctx", {}).get(key, ""))
    return "格式不正确"  # value_error 等的 ctx 和 msg 可能带输入内容，不透传


def validation_response(exc):
    """校验失败：中文说明带字段名，不回显输入。整段 JSON 解析失败时 loc 里的是字节位置，不算字段。"""
    from fastapi.responses import JSONResponse

    errors = []
    for error in exc.errors():
        location = error["loc"][1:] if error["loc"] and error["loc"][0] in ("body", "query", "path", "header", "cookie") else error["loc"]
        field = "" if error["type"] == "json_invalid" else ".".join(str(part) for part in location)
        errors.append({"field": field, "message": _validation_message(error)})
    shown = "；".join(f"{e['field']} {e['message']}".strip() for e in errors[:3])
    more = f"等 {len(errors)} 处" if len(errors) > 3 else ""
    return JSONResponse(status_code=422, content={
        "detail": f"提交的内容格式不正确：{shown}{more}", "code": "validation_error", "errors": errors,
    })
