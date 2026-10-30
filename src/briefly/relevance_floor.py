"""知识库相关度下限：按 Embedding 指纹登记。

不同模型的相似度尺度不同，换模型后沿用旧数字不会报错，只会让路由和检索默默变差，
所以下限跟着模型指纹走。意图漏斗的知识库路由和检索结果的相关度下限都通过 kb_floor 取值，
保证两边对“相关”的口径一致。换模型后用 tests/eval/intent_eval.py --kb-root <库> --sweep
重新标定，再把结果登记到 REGISTERED。
"""
import logging

from . import config
from .knowledge.service import fingerprint

log = logging.getLogger(__name__)

# Embedding 指纹（knowledge.service.fingerprint(identity)，与索引代次里的 embedding_fingerprint 相同）→ 下限
REGISTERED: dict[str, float] = {
    # BAAI/bge-small-zh-v1.5，revision 46fbe35，512 维，CLS，max_tokens 512；
    # 在 v3 切片的 W3-selected 库上用 intent_eval.py --sweep 标定（2026-10）
    "7087fad7875417206de4edee788efe164bdaa69f2e8884b2ab6adf763be5fb07": 0.50,
}


def embedding_fingerprint(identity: dict | str) -> str:
    """identity 可以是 Embedding 身份对象（编码器的 identity 或索引代次里存的身份），也可以是已算好的指纹。"""
    if isinstance(identity, str):
        return identity
    identity = dict(identity)
    identity.pop("index_contract", None)  # 索引代次附加的来源契约不属于模型身份
    return fingerprint(identity)


def kb_floor(identity: dict | str | None) -> float | None:
    """当前模型的相关度下限。配置了 BRIEFLY_INTENT_KB_THRESHOLD 时用配置值；
    否则查登记表，没有登记（或身份未知）时返回 None，由调用方回退。"""
    if config.INTENT_KB_THRESHOLD is not None:
        return config.INTENT_KB_THRESHOLD
    if identity is None:
        return None
    return REGISTERED.get(embedding_fingerprint(identity))


def describe(identity: dict | str) -> str:
    if isinstance(identity, str):
        return f"指纹 {identity[:12]}"
    return f"{identity.get('model', '未知模型')}（指纹 {embedding_fingerprint(identity)[:12]}）"


def missing_floor_message(identity: dict | str) -> str:
    return (f"Embedding {describe(identity)} 没有登记知识库相关度下限，知识库路由已跳过（请求交给助理判断）。"
            "请用 tests/eval/intent_eval.py --kb-root <库> --sweep 标定后登记到 briefly.relevance_floor.REGISTERED，"
            "或临时设置 BRIEFLY_INTENT_KB_THRESHOLD")


def check_indexes(engine) -> list[str]:
    """启动时检查已就绪索引所用的模型是否都有下限；返回并记录没有登记值的模型说明。只读数据库，不加载模型。"""
    if config.INTENT_KB_THRESHOLD is not None:
        log.info("知识库相关度下限使用配置覆盖值 %.2f", config.INTENT_KB_THRESHOLD)
        return []
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from .knowledge.models import IndexGeneration

    with Session(engine) as s:
        identities = s.scalars(select(IndexGeneration.embedding_identity).where(IndexGeneration.status == "ready"))
        missing = {embedding_fingerprint(i): i for i in identities if kb_floor(i) is None}
    messages = [missing_floor_message(identity) for identity in missing.values()]
    for message in messages:
        log.warning(message)
    return messages
