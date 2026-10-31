"""疑似重复阈值标定（二期）：原创样本上看“改稿”与“不同文档”的句子指纹重合度分布。只在本地计算，不调用模型。

样本：
- 正例（应提示）：w3_e2e 虚构资料 8 份，各按固定随机种子生成改稿——改数字、改写句子、增删段落、调换章节顺序，
  改动比例 10%～60%；外加“只保留前半部分”的节选稿。
- 反例（不应提示）：虚构资料两两之间（同一家虚构公司、行文风格与套话相近）；以及同一模板、内容不同的原创文档
  （两次会议纪要、两个门店的周报、两份不同岗位的入职指引）。

用法：python tests/eval/link_threshold_calibration.py
"""
import json
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from briefly.links import name_similarity, overlap, sentence_keys  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "w3_e2e"


def text_of(doc: dict) -> str:
    lines = [f"# {doc['title']}"]
    for kind, value in doc["blocks"]:
        if kind in ("h1", "h2"):
            lines.append(("## " if kind == "h1" else "### ") + value)
        elif kind == "p":
            lines.append(value)
        else:
            lines.append("| " + " | ".join(value[0]) + " |")
            lines.append("|" + "---|" * len(value[0]))
            lines += ["| " + " | ".join(r) + " |" for r in value[1:]]
    return "\n".join(lines)


def revise(text: str, ratio: float, seed: int) -> str:
    """按比例改动行：一半改数字或措辞，一半删掉或插入新句；另把两段调换顺序。"""
    rng = random.Random(seed)
    lines = text.splitlines()
    body = [i for i, line in enumerate(lines) if len(line) > 12 and not line.startswith("#")]
    for i in rng.sample(body, max(1, int(len(body) * ratio))):
        action = rng.random()
        if action < 0.35:
            lines[i] = "".join(str((int(c) + 3) % 10) if c.isdigit() else c for c in lines[i]) + "（已调整）"
        elif action < 0.6:
            lines[i] = lines[i][: len(lines[i]) // 2] + "，具体以部门最新通知为准。"
        elif action < 0.8:
            lines[i] = ""
        else:
            lines[i] = lines[i] + "\n补充说明：本条自下季度起执行，执行中的问题请反馈至综合管理部门。"
    if len(body) > 4:
        a, b = rng.sample(body, 2)
        lines[a], lines[b] = lines[b], lines[a]
    return "\n".join(lines)


TEMPLATES = [
    ("项目周会纪要-第12周.docx",
     "# 项目周会纪要\n## 参会人\n张楠、李卫、王小雨\n## 上周进展\n登录模块完成联调，支付回调仍偶发超时。\n压测脚本已写完，周三开始在预发环境执行。\n"
     "## 本周计划\n修复支付回调超时问题，目标周五前提测。\n完成订单分页方案评审。\n## 风险\n第三方短信供应商合同下周到期，需提前续签。",
     "项目周会纪要-第13周.docx",
     "# 项目周会纪要\n## 参会人\n张楠、李卫、赵晨\n## 上周进展\n支付回调超时已定位为连接池配置过小，已修复并提测。\n订单分页方案评审通过。\n"
     "## 本周计划\n预发环境压测，关注峰值时数据库连接数。\n启动退款流程的接口设计。\n## 风险\n测试同学下周请假两天，提测可能顺延。"),
    ("一号店周报.md",
     "# 门店周报\n## 营业情况\n本周营业额十二万三千元，环比上升百分之六。\n午高峰平均等待时间九分钟。\n## 人员\n新入职打包员一名，已完成培训。\n"
     "## 问题\n冷柜温度偶尔偏高，已报修。\n## 下周计划\n上线第二台出餐提示屏。",
     "二号店周报.md",
     "# 门店周报\n## 营业情况\n本周营业额八万七千元，环比下降百分之三，受周边道路施工影响。\n晚高峰外卖单量增加明显。\n## 人员\n一名店员离职，正在招聘。\n"
     "## 问题\n外卖打包台空间不足，高峰时出餐拥堵。\n## 下周计划\n调整打包台位置，增加一名兼职。"),
    ("入职指引-研发岗.docx",
     "# 新员工入职指引\n## 第一天\n到前台领取工牌和笔记本电脑，由导师带领熟悉工位。\n## 账号开通\n开通代码仓库、持续集成平台和测试环境的访问权限。\n"
     "## 第一周\n阅读系统架构文档，完成一个入门任务并提交代码评审。\n## 试用期考核\n以代码质量和需求交付为主要考核内容。",
     "入职指引-销售岗.docx",
     "# 新员工入职指引\n## 第一天\n到前台领取工牌，参加销售部门的晨会。\n## 账号开通\n开通客户关系管理系统和报价工具的账号。\n"
     "## 第一周\n跟随资深销售拜访两家重点客户，熟悉产品报价规则。\n## 试用期考核\n以新签合同金额和客户回访质量为主要考核内容。"),
    # 同一套制度模板：总则、附则的套话完全相同，正文不同（反例里最难的一类）
    ("差旅费管理办法.docx",
     "# 差旅费管理办法\n## 第一章 总则\n第一条 为规范公司管理，提高工作效率，根据国家有关规定，结合公司实际情况，制定本办法。\n"
     "第二条 本办法适用于公司全体正式员工及实习生。\n## 第二章 出差申请\n第三条 员工出差须提前两个工作日在系统中提交申请，经部门负责人审批后方可出行。\n"
     "第四条 住宿标准按城市类别执行，一类城市每晚不超过六百元。\n第五条 交通工具以高铁二等座为主，特殊情况需事先说明。\n"
     "## 第三章 附则\n第六条 本办法由财务部负责解释。\n第七条 本办法自发布之日起施行，原有相关规定同时废止。",
     "办公用品管理办法.docx",
     "# 办公用品管理办法\n## 第一章 总则\n第一条 为规范公司管理，提高工作效率，根据国家有关规定，结合公司实际情况，制定本办法。\n"
     "第二条 本办法适用于公司全体正式员工及实习生。\n## 第二章 领用\n第三条 办公用品每月第一周集中领用，由各部门行政助理汇总需求。\n"
     "第四条 单价超过五百元的用品须经部门负责人审批。\n第五条 离职员工须归还未用完的耐用品。\n"
     "## 第三章 附则\n第六条 本办法由行政部负责解释。\n第七条 本办法自发布之日起施行，原有相关规定同时废止。"),
    ("周会纪要模板-市场部.md",
     "# 部门周会纪要\n会议由部门负责人主持，全体成员参加。\n请各位在会前更新各自负责事项的进展。\n## 本周事项\n完成秋季促销活动的渠道投放计划。\n"
     "与两家媒体确认了软文排期。\n## 待办\n下周一前提交活动预算明细。\n下次会议时间另行通知，如有变动由行政助理提前告知。",
     "周会纪要模板-客服部.md",
     "# 部门周会纪要\n会议由部门负责人主持，全体成员参加。\n请各位在会前更新各自负责事项的进展。\n## 本周事项\n客诉平均响应时间缩短到十五分钟。\n"
     "新上线的自助退款入口使用率偏低，需要优化引导文案。\n## 待办\n整理本月高频问题清单。\n下次会议时间另行通知，如有变动由行政助理提前告知。"),
]


def main() -> None:
    docs = json.loads((FIXTURES / "documents.json").read_text(encoding="utf-8"))["documents"]
    texts = {d["title"]: text_of(d) for d in docs}
    keys = {t: sentence_keys(x) for t, x in texts.items()}
    positives, negatives = [], []
    for n, (title, text) in enumerate(texts.items()):
        for ratio in (0.1, 0.2, 0.3, 0.45, 0.6):
            revised = revise(text, ratio, seed=n * 100 + int(ratio * 100))
            positives.append((f"{title} 改稿{int(ratio * 100)}%", overlap(keys[title], sentence_keys(revised)),
                              name_similarity(title + ".docx", title + "（修订稿）.docx")))
        half = "\n".join(text.splitlines()[: len(text.splitlines()) // 2])
        positives.append((f"{title} 前半节选", overlap(keys[title], sentence_keys(half)), 1.0))
    titles = list(texts)
    for i, a in enumerate(titles):
        for b in titles[i + 1:]:
            negatives.append((f"{a} vs {b}", overlap(keys[a], keys[b]), name_similarity(a, b)))
    for a_name, a, b_name, b in TEMPLATES:
        negatives.append((f"{a_name} vs {b_name}", overlap(sentence_keys(a), sentence_keys(b)), name_similarity(a_name, b_name)))
    from briefly.links import NAME_ASSISTED_OVERLAP, NAME_SIMILAR, SUGGEST_OVERLAP, is_suggested
    hit = sum(is_suggested(s, n) for _, s, n in positives)
    renamed = sum(is_suggested(s, 0.0) for _, s, _ in positives)
    false = [name for name, s, n in negatives if is_suggested(s, n)]
    print(f"当前阈值：重合度≥{SUGGEST_OVERLAP}，或重合度≥{NAME_ASSISTED_OVERLAP}且文件名相似度≥{NAME_SIMILAR}")
    print(f"正例提示 {hit}/{len(positives)}（文件名不相似时 {renamed}/{len(positives)}）；反例误提示 {len(false)}/{len(negatives)} {false}")
    for label, rows in (("正例（改稿）", positives), ("反例（不同文档）", negatives)):
        scores = sorted(s for _, s, _ in rows)
        print(f"{label} {len(rows)} 对：重合度 最低 {scores[0]:.3f} 中位 {scores[len(scores) // 2]:.3f} 最高 {scores[-1]:.3f}")
        for name, score, names in sorted(rows, key=lambda r: r[1])[:5 if label.startswith("正") else None]:
            if label.startswith("正") or score > 0.05 or names > 0.5:
                print(f"  {score:.3f}  名称{names:.2f}  {name}")


if __name__ == "__main__":
    main()
