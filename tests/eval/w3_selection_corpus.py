"""原创虚构W3选型语料：生成PDF，再由正式解析入口核对；问题标签不进入检索文本。"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[2]
DOCS=[]
CASES=[]


def document(ident,family,category,split,title,sections,table=None):
    DOCS.append(dict(id=ident,family=family,kb=category,split=split,title=title,sections=sections,table=table,
                     authorization="本项目原创虚构评测资料，可公开复现；不对应真实企业、产品、人物或制度"))


document("cl-old","office-chenglan","office","validation","澄岚研发采购制度 v1（历史）",[
 ("适用与版本","本制度由虚构的澄岚研究所用于内部软件测试，自2026年7月1日施行。2026年10月1日起被v2替代，仅用于追溯旧申请。不得把历史审批额度用于新版生效后的采购。制度覆盖研发试制所需的普通耗材，不覆盖固定资产、个人福利或紧急抢修。"),
 ("普通采购","旧版规定，含税总额不足3000元的耗材由项目负责人批准；达到3000元的申请均须部门主管与财务共同批准。拆成多张申请单不能改变同一采购事项的合计金额。审批完成后由采购专员询价，申请人不得自行代表研究所签署合同。"),
 ("验收与报销","货物到达后由使用部门登记数量与型号，验收人应与采购经办人分开。缺少型号或数量记录时，财务退回补充材料。采购获批不代表已验收，验收通过也不证明供应商已经收到款项。旧版没有承诺从批准到付款的固定天数。"),
 ("历史保留","旧审批单保留原申请日期、当时适用版本和签字人。制度替代不追溯修改已批准的原单据；旧申请新增采购事项时，新增部分另按实际申请日期选择版本。本文件未记载任何真实供应商的银行账号。")])
document("cl-new","office-chenglan","office","validation","澄岚研发采购制度 v2",[
 ("范围与生效","本制度为原创虚构资料，自2026年10月1日生效，替代澄岚研发采购制度v1。适用于国内研发耗材采购；进口仪器与固定资产不在本流程内。所有审批额度均按同一采购事项含税总额计算，不能通过分单规避合并审批。"),
 ("分级审批","新版规定，不足5000元由项目负责人批准；5000元至不足20000元由部门主管和财务共同批准；达到20000元须在前述两人批准后再交所长批准。审批权限只是内部授权，不代表采购需求已确认、货物已验收或付款已发生。"),
 ("收货与付款","验收需要采购单、送货单与实物型号数量三方核对，发现短缺时先登记差异，不能为了赶付款期限把数量补成已收齐。财务只在验收记录与有效发票齐全后安排付款，本文没有规定具体到账日期。供应商催款不构成跳过验收的理由。"),
 ("紧急申请","紧急抢修耗材另按例外说明处理，本制度的普通额度表不能作为免审批依据。申请人需保存故障发生时间、停机影响和必要采购范围。先口头沟通可用于协调，但正式授权仍以例外说明要求的书面记录为准。"),
 ("归档责任","采购专员保管询价和订单资料，使用部门保管验收原始记录，财务保存发票与付款凭证。三类记录通过采购事项编号关联。采购专员只能说明订单进展，不能在未取得银行回执时自行确认供应商已到账。")],table=[['含税事项总额','普通流程批准人'],['不足5000元','项目负责人'],['5000元至不足20000元','部门主管和财务'],['达到20000元','部门主管、财务、所长']])
document("cl-exception","office-chenglan","office","validation","澄岚紧急抢修采购例外说明",[
 ("启动条件","本说明仅用于设备突发故障已经造成试验停机的抢修耗材采购，不适用于计划性备货、办公室装修或申请人忘记提前下单。使用部门必须记录停机影响、故障时间和拟购耗材与故障的关系，不得只填紧急二字作为理由。"),
 ("书面授权","紧急抢修必须在下单前获得值班部门主管与财务值班人的书面同意；含税总额达到20000元时，还须取得所长书面同意。电话同意后补签不是本说明允许的默认做法。任何一位批准人联系不上时，应升级协调，不能自行绕过。"),
 ("事后材料","经批准下单后，申请人应在恢复生产后的2个工作日内补齐故障记录、原授权截图、验收记录与发票。补齐材料不是补办事前授权，不能使未经授权的订单自动变为合规。恢复生产时间不等于货物送达时间，应分别记录。"),
 ("边界说明","本说明没有给出实习生独立签单权限，也没有允许将所有后续备货计入本次抢修。本文只描述审批和归档要求，不认定任何实际事件已经获批、停机已经恢复或供应商已经收款。")])

document("pc-plan","project-bochuan","project","dev","泊川知识门户项目启动纪要",[
 ("项目范围","泊川项目是虚构的内部知识门户试点。首期交付制度检索、文档版本展示与来源打开，不包含录音转写、扫描件OCR或跨企业账号互通。计划支持两个部门试用；试点覆盖范围不等于已经在全公司上线，会议也没有确认最终使用人数。"),
 ("分工与计划","启动会计划林舟在2026年11月6日前完成目录迁移，郑宁在11月9日前完成权限回归，孟禾负责验收清单。目录迁移与权限回归是不同工作，不能因为文件已上传就把权限测试标成通过。本文记录的是原计划，后续经批准的变更应单独核对。"),
 ("预算与采购","项目预算上限为12万元，分为试点环境、迁移支持和验收协作三类。预算上限不是已经发生的费用，也不是对供应商的付款承诺。采购合同须另走审批流程；本纪要没有列出合同编号、发票或银行回执，不能据此编出实付金额。"),
 ("阶段产物","每批迁移需保留原路径、目标文档ID、版本和校验摘要，以便定位差异。失败文件进入问题清单，不允许仅统计成功数量后宣布全部迁移完成。验收清单应逐项记录执行人、日期、结果和必要证据，空白项仍属于未验证。"),
 ("发布约束","原计划要求目录校验与权限回归均通过，且验收负责人孟禾签字后才允许发布。发布准备、发布批准和实际发布应分开记录。会议未授权自动发送通知，也没有决定正式服务的可用性承诺；这些内容不能由试点日期推算。")])
document("pc-change","project-bochuan","project","dev","泊川知识门户批准变更单 B-07",[
 ("变更效力","变更单B-07于2026年11月3日经项目负责人批准，仅调整目录迁移期限与试点附件格式范围。它替代启动纪要中相应的原计划，不改变预算上限、权限回归负责人或发布前签字条件。批准变更只证明计划发生调整，不证明相关工作已经完成。"),
 ("延期与负责人","因两批源文件目录重名，目录迁移截止日从11月6日调整为11月10日，负责人仍为林舟。权限回归仍由郑宁在11月9日前完成。两项工作可能交叉进行，不能把其中一个新日期复制到另一项，也不能自行把迁移阻塞标记为已解决。"),
 ("格式范围","试点新增可选中文字的PDF附件，原有DOCX保留；扫描件PDF与OCR仍不在首期范围。格式支持不表示所有历史文件都已经解析成功，失败附件必须继续留在问题清单，不能通过改扩展名让其变为已支持。"),
 ("生效后的记录","项目助理需将变更单与原纪要同时归档，保留原期限和批准日期，禁止覆盖原会议记录使旧决定不可追溯。对外展示进度时注明当前执行的是B-07计划。本文未给出新的实际发布日期，也没有列出目录重名问题的最终处置结果。")])
document("pc-accept","project-bochuan","project","dev","泊川知识门户验收与回退预案",[
 ("发布闸门","执行当前批准计划时，目录校验、权限回归和验收负责人孟禾签字三项必须同时满足才能发布。验收材料至少包含受限文档的越权反例，不能只用管理员账号演示成功。任一项未验证就保持待验收，不以启动纪要中的预计日期代替验收结果。"),
 ("回退触发","试点发布后若连续8分钟来源打开失败率超过3%，由值班人陆岑启动回退。触发条件同时包含持续时间和失败比例，不能只看到一次失败就认定达到阈值。数据不足8分钟时应继续观察并记录，不补造监控样本。"),
 ("执行顺序","回退时先暂停新增导入，再恢复上一个有效索引代次，最后核对受限文档权限。回退不删除问题清单或失败任务记录，结果未知的导出不能自动重发。值班人记录开始时间、恢复代次、核对结果和仍未恢复的功能。"),
 ("恢复确认","恢复索引后需重新验证来源可打开及范围隔离，不能只凭服务进程存活宣布恢复。本文未承诺固定恢复分钟数，也没有写明试点当前已经触发回退。项目通知应由人工确认收件范围，预案本身不是发送授权。")])

document("sh-base","project-shanhe","project","validation","杉河客户档案迁移基线方案",[
 ("目标与数据边界","杉河是虚构的客户档案迁移项目，将三个事业部的历史档案迁入内网库。计划总量约20万条，实际数量以每批清点表为准；20万是规划估计，不是已经完成的数量。联系人字段只能进入授权内网环境，不得发送到外部生成模型或公共诊断日志。"),
 ("批次与身份","基线方案把每批上限设为5000条，由数据负责人许砚确认映射，实施人顾青执行导入。迁移保持原客户编号作为追溯键，不按姓名合并同名客户。重复编号、缺失授权或字段映射未确认的记录必须隔离，不能为了凑足批量直接覆盖。"),
 ("原窗口与抽样","原计划在2026年11月14日20时至22时冻结源库写入，完成全量校验后再开放。每批随机抽查5%的档案，并对所有重复编号逐条核对。随机抽样与重复编号全量核对是两个要求，不能用抽样比例代替异常记录处理。"),
 ("完成口径","导入任务结束、校验通过和业务签收分别记录。只有数据负责人许砚与业务代表乔安共同签收，批次才标记交付完成。计划中的冻结窗口不证明已实际执行，也不保证窗口结束时所有问题自动消失。")])
document("sh-decision","project-shanhe","project","validation","杉河试迁移评审决定 S-12",[
 ("决定依据","S-12是原创虚构的评审记录，日期为2026年11月11日。试迁移发现跨事业部重复编号风险，评审决定先开展小批试点，不按原5000条批次直接进入正式迁移。本记录是批准的执行调整，风险描述不是已完成整改的证明。"),
 ("批量与新窗口","试点阶段每批上限改为2000条，源库冻结窗口调整为11月16日19时至21时。本决定仅覆盖试点，正式扩大批量需另行批准；不得把2000条反向写成基线方案原有数字，也不得自动推算所有20万条都在该窗口完成。"),
 ("抽样保留","5%的随机抽查比例保留，重复编号仍逐条核对。任何重复编号涉及多个事业部时，先由许砚确认身份归属，再决定合并或保留独立档案；顾青没有自行把同名客户视为同一人的授权。映射确认记录需要和原始编号一起保存。"),
 ("对外状态","目前只批准试点调整，未记录正式迁移已完成或业务已签收。项目助理可以发布待执行计划，但不得把风险处置意向写成完成结果。本文没有新的预算、付款金额或正式上线日期。")])
document("sh-recovery","project-shanhe","project","validation","杉河试点校验与故障恢复规程",[
 ("失败判定","任一批次若发现授权缺失、编号错误归并或抽查失败，保持该批次未交付并暂停后续批次。不得只删除失败记录后重新计算通过率，也不能把校验任务运行完成视为档案内容已核实。暂停范围必须包含同一映射规则影响的后续批次。"),
 ("恢复前置","回退前由许砚确认恢复目标快照，顾青先停止导入，再验证快照可读，最后执行恢复。未完成恢复目标确认时不得直接回退；恢复动作不自动清空失败证据，原批次编号与错误归属记录均需保留用于后续复核。"),
 ("未知写入","网络超时后，导入结果视为未知。先按批次幂等键查询回执并核对目标记录，再决定重试；不能仅凭客户端没有收到响应就整批重放。查询回执失败时保持待核对，本文未授权改用新的幂等键绕过未知结果。"),
 ("恢复后交付","快照恢复后，重新执行映射校验、抽样和异常编号全量核对，仍由许砚与乔安共同签收。恢复成功不等于业务签收完成。本文没有承诺零数据丢失、自动补发通知或固定故障恢复时长。")])

document("lc-old","manual-lance","manual","dev","岚册工作空间 1.0 历史手册",[
 ("版本定位","岚册工作空间是原创虚构产品。本文仅适用1.0，已由2.0手册替代，用于解释历史任务。不能把旧错误码含义和格式限制套到新版。系统界面是否相似不改变版本归属，查故障前先核对任务详情中的产品版本。"),
 ("旧上传规则","1.0单个文件上限为10MB，仅支持TXT。编辑者可创建上传任务，查看者只能读取已有完成结果。旧版没有PDF读取或扫描件识别能力，不能通过把PDF改成TXT扩展名让其获得支持。文件大小指上传文件本身，不是压缩后目录的总显示大小。"),
 ("历史错误码","1.0中的E201表示单文件超过10MB，应缩小文件再提交；E403表示没有编辑权限。本文不定义E504，也不承诺每次失败都可以安全重试。旧错误说明只用于1.0任务，新版故障必须查新版手册或维护说明。"),
 ("历史维护","升级不改变已下载文件的内容，旧任务记录保留原版本和原引用。本文没有说明1.0任务可以无损迁移为2.0任务，也没有列出客户电话或付费套餐。旧下载链接过期不等于文件已从用户电脑删除。")])
document("lc-new","manual-lance","manual","dev","岚册工作空间 2.0 操作手册",[
 ("适用范围","本文适用虚构产品岚册工作空间2.0。上传支持TXT、DOCX与可选中文字的PDF，扫描件PDF不受支持；没有OCR能力。用户应核对文件内容类型，不能只靠扩展名判断可读取性，也不能把部分解析警告忽略后宣称文件完整可用。"),
 ("上传与权限","2.0单个文件上限为32MB。上传前必须登录并拥有目标工作空间的编辑权限。查看权限只能读取已完成结果，不包含上传或删除权限。权限不足时应申请编辑授权，不能更换文件名或改链接参数绕过。"),
 ("导出CSV","导出CSV必须同时满足查看权限和任务状态为完成。操作顺序是打开任务详情、点击导出、选择CSV、确认下载。处理中、取消和失败状态不能导出新结果。导出按钮可见不证明当前任务已有可下载的完整结果。"),
 ("取消与重建","取消任务后需重新提交才能生成新的结果；取消不会删除已经下载到本地的文件。重新提交创建新的任务身份，原任务记录仍可用于追溯。本文没有承诺取消后一定退回费用，也没有允许自动重放结果未知的任务。"),
 ("状态提示","可检索表示当前有效索引已建立，上传完成只表示文件接收成功。解析失败或索引维护期间的文件不能冒充可检索；问答范围应明确展示未就绪文件。本文没有说明企业管理员可以不经授权读取所有工作空间。")],table=[['任务状态','能否新导出CSV'],['完成且有查看权限','可以'],['处理中','不可以'],['取消或失败','不可以']])
document("lc-errors","manual-lance","manual","dev","岚册工作空间 2.0 错误与维护",[
 ("登录与超限","2.0中的E201表示登录会话过期，应重新登录后核对原任务，再决定是否重试；它不再表示文件超限。E413表示文件超过32MB，应缩小或拆分文件。处理前必须核对版本，不能沿用1.0中E201的大小限制解释。"),
 ("未知执行结果","E504表示请求超时且执行结果未知。先刷新任务详情并查询回执，确认原任务状态后再决定重试，不能直接反复点击创建。查询不到回执时保留待核对状态，不把暂时不可见等同于服务端完全没有执行。"),
 ("维护期间","维护时已有任务详情可查看，新文档问答暂停。维护结束后需逐个确认文档状态为可检索，再恢复这些文档的问答。界面解除维护提示不代表所有失败文档已经重建成功，原有错误记录不能被清空冒充恢复。"),
 ("保留期限","导出下载链接保留24小时，任务结果保留30天。链接到期可在任务详情重新生成，但已删除任务不能恢复下载。上述期限不控制用户已下载副本，也没有承诺7×24小时人工客服或故障赔付。")])

document("xj-access","manual-xunji","manual","validation","循迹工作台 3.2 访问与分享手册",[
 ("版本与角色","循迹工作台是原创虚构的协作产品，本文只适用3.2。空间管理员可管理成员，文档所有者可设置自己文档的分享，普通编辑者可修改已授权正文。管理员角色不自动取得其他空间的文档读取权，必须另有目标空间授权。"),
 ("外部分享条件","创建外部分享链接必须同时满足文档所有者身份与空间允许外部分享。链接最长有效7天，必须设置提取码；包含客户联系人字段的文档禁止外部分享。编辑权限、管理员身份或已经知道原文档地址都不能替代这三个条件。"),
 ("撤销与副本","所有者撤销链接后，后续访问应被拒绝；此前已下载的副本不受链接撤销控制。撤销记录包含执行人、时间和链接ID，不把撤销成功描述为已删除所有外部副本。链接过期与主动撤销是不同事件，应分别显示。"),
 ("搜索边界","搜索只覆盖当前明确选中的空间和有效索引文档，归档区默认不参与问答。资料可读取不代表此次请求已经检索过全部空间，未建索引文档需可见提示。本文没有提供跨空间任意搜索的超级管理员接口。")])
document("xj-storage","manual-xunji","manual","validation","循迹工作台 3.2 存储与恢复说明",[
 ("配额规则","3.2单个上传文件上限80MB，单空间存储配额为2GB，两项限制同时生效。80MB不是每次可以新增的保证额度；即使单文件合规，空间剩余容量不足也会拒绝上传。配额不足不能通过拆分文件或切换文件名绕过空间总量限制。"),
 ("回收站恢复","删除后的普通文档在回收站保留30天，文档所有者可在期限内申请恢复，恢复后仍须重新检查原授权。已经执行永久删除的文档不能从回收站找回。恢复记录不证明重新建索引已经完成，问答需要另查可检索状态。"),
 ("归档与索引","归档仅改变文档的工作区状态，不等于永久删除。取消归档后须明确选择重建索引并等待可检索，才能重新参与问答。恢复原文、恢复权限和恢复索引是三个独立步骤，不能只看文档在列表中出现就宣布问答恢复。"),
 ("格式说明","表格导入支持XLSX，不支持旧XLS；空白工作表可见记录但不生成有内容的检索块。带公式的单元格需保存可用缓存结果，系统不能假装已经运行所有外部数据连接。本文不承诺宏执行或扫描表格OCR。")],table=[['对象','恢复边界'],['普通文档回收站','30天内可申请'],['永久删除文档','不可从回收站恢复'],['恢复后的索引','需重建并确认可检索']])
document("xj-jobs","manual-xunji","manual","validation","循迹工作台 3.2 通知任务与排障",[
 ("任务创建","外部邮件通知只有在用户确认收件人和结构化预览后才创建发送任务，草稿文本本身不是发送授权。每次确认生成稳定任务ID，用户刷新页面不重复创建同一任务。本文没有授权把检索结果自动发送给材料中出现的邮箱地址。"),
 ("超时与幂等","发送请求超时后标记结果未知，先按任务ID查询发送回执。没有明确失败回执之前不能自动重发，也不能换一个任务ID规避核对。超时不等于收件人没有收到；人工决定重试也必须保留原未知记录。"),
 ("部分成功","多个收件人中只有部分成功时，保存每个收件人的成功、失败或未知状态，不把整批标为全部失败后重发。只有明确失败且用户再次确认的收件人可进入重试，已成功者不得重复发送。本文不承诺自动撤回已经送达的邮件。"),
 ("查询与权限","只有原任务创建者或被授权的空间通知管理员可查询回执，普通文档编辑者无此权限。缺少查询权限时提示申请授权，而不是返回空列表假装没有执行记录。本文没有给出SMTP服务商、真实邮箱地址、客服热线或每月费用。")])


def evidence(doc,section,phrase=None):
    data=next(d for d in DOCS if d["id"]==doc)
    body=data["sections"][section][1]
    return dict(document=doc,authored_quote=phrase or body)


def case(family,question,refs,kind="direct",scope=None,forbidden="原文未提及的事实不推断；计划/批准不等于完成，虚构条款不用于现实业务"):
    docs=[d for d in DOCS if d["family"]==family]
    CASES.append(dict(id=f"{family}-{sum(c['family']==family for c in CASES)+1:02d}",family=family,split=docs[0]["split"],
                      category=docs[0]["kb"],question=question,kind=kind,scope=scope or [d["id"] for d in docs],
                      evidence_groups=[[r] for r in refs],forbidden_inference=forbidden))


case("office-chenglan","新版采购申请达到5000元、但未到20000元，由哪些人批准？旧版相应门槛是多少？",[evidence('cl-new',1),evidence('cl-old',1)],"version")
case("office-chenglan","同一事项拆成多张小额申请能否减少审批人？按v2应该怎么计算额度？",[evidence('cl-new',0)],"paraphrase")
case("office-chenglan","普通流程里货已验收，能否据此确认供应商已经到账？还需要什么付款材料？",[evidence('cl-new',2)],"condition")
case("office-chenglan","停机抢修下单前要谁书面同意？达到20000元时还需要谁？",[evidence('cl-exception',1)],"number")
case("office-chenglan","设备恢复后多久补材料，补材料能替代下单前的授权吗？",[evidence('cl-exception',2)],"condition")
case("office-chenglan","申请人忘记提前备货，能直接用抢修例外下单吗？",[evidence('cl-exception',0)],"paraphrase")
case("office-chenglan","查询旧单据时应保留什么版本信息，新增采购还能按旧单额度办吗？",[evidence('cl-old',3),evidence('cl-new',0)],"multi_evidence")
case("office-chenglan","订单资料、验收原始记录和付款凭证分别由谁保存？采购专员能自行宣布到账吗？",[evidence('cl-new',4)],"role")
case("office-chenglan","澄岚研究所开户银行的真实账号是多少？",[],"no_answer")
case("office-chenglan","仅看普通采购v2，抢修恢复后的补充材料具体限几天？",[],"out_of_scope",scope=['cl-new'])

case("project-bochuan","泊川目录迁移现在按哪个截止日执行，负责人有没有换？原计划是什么？",[evidence('pc-plan',1),evidence('pc-change',1)],"version")
case("project-bochuan","项目预计花12万，是否代表12万已经付给供应商？",[evidence('pc-plan',2)],"paraphrase")
case("project-bochuan","计划已经延期，权限回归日期和发布签字条件是否也一起延期或取消了？",[evidence('pc-change',0),evidence('pc-change',1),evidence('pc-accept',0)],"multi_evidence")
case("project-bochuan","新增PDF支持后，扫描版PDF也能自动读了吗？",[evidence('pc-change',2)],"condition")
case("project-bochuan","怎样才达到回退阈值，谁负责执行？",[evidence('pc-accept',1)],"number")
case("project-bochuan","回退时暂停导入、恢复索引、校验权限的先后顺序是什么？未知导出能自动重发吗？",[evidence('pc-accept',2)],"procedure")
case("project-bochuan","如果只用管理员账号演示成功，可以发布给试点用户吗？",[evidence('pc-accept',0)],"paraphrase")
case("project-bochuan","文件上传完就能宣布整批迁移完成吗，失败文件放哪里？",[evidence('pc-plan',3)],"condition")
case("project-bochuan","泊川目前真实已支付金额是多少，供应商发票号是什么？",[],"no_answer")
case("project-bochuan","只查启动纪要，B-07最终批准的新迁移截止日是哪天？",[],"out_of_scope",scope=['pc-plan'])

case("project-shanhe","杉河规划的20万条档案能当成已迁移完成量吗？联系人能发给外部模型排查吗？",[evidence('sh-base',0)],"paraphrase")
case("project-shanhe","试点每批上限和冻结窗口现在是什么，原批量与原窗口分别是多少？",[evidence('sh-base',1),evidence('sh-base',2),evidence('sh-decision',1)],"version")
case("project-shanhe","新版试点是否取消了重复编号逐条核对，谁有权决定身份归属？",[evidence('sh-decision',2)],"condition")
case("project-shanhe","导入时没有响应，可以换个幂等键整批重新导入吗？应先做什么？",[evidence('sh-recovery',2)],"paraphrase")
case("project-shanhe","快照恢复需要哪位先确认目标，实际操作顺序是什么？",[evidence('sh-recovery',1)],"role")
case("project-shanhe","若抽查失败，能删掉错误记录后把本批算通过并继续后续批次吗？",[evidence('sh-recovery',0)],"condition")
case("project-shanhe","索引或快照恢复成功后是否已经交付，仍需谁共同签收？",[evidence('sh-recovery',3),evidence('sh-base',3)],"multi_evidence")
case("project-shanhe","评审已经批准试点调整，能对外写成正式迁移完成和业务签收完成吗？",[evidence('sh-decision',3)],"status")
case("project-shanhe","杉河本次故障承诺几分钟恢复，真实预算是多少？",[],"no_answer")
case("project-shanhe","只查基线方案，S-12的新冻结时间是什么？",[],"out_of_scope",scope=['sh-base'])

case("manual-lance","岚册2.0看到E201，应重新登录还是缩小文件？1.0中同码原来表示什么？",[evidence('lc-errors',0),evidence('lc-old',2)],"version")
case("manual-lance","岚册2.0的单文件上限和上传权限是什么，查看者能直接上传吗？",[evidence('lc-new',1)],"condition")
case("manual-lance","用户想下载CSV，需要满足什么条件，操作顺序是什么？",[evidence('lc-new',2)],"procedure")
case("manual-lance","取消任务以后，已下载到电脑的文件会被删除吗，如何生成新结果？",[evidence('lc-new',3)],"paraphrase")
case("manual-lance","E504之后一直刷新不到回执，能认为没执行并重新创建吗？",[evidence('lc-errors',1)],"condition")
case("manual-lance","维护结束后所有文档是否自动恢复问答，要核对哪个状态？",[evidence('lc-errors',2),evidence('lc-new',4)],"multi_evidence")
case("manual-lance","下载链接过期和任务结果删除是同一个期限吗？",[evidence('lc-errors',3)],"number")
case("manual-lance","把扫描PDF扩展名改成TXT能让岚册2.0识别吗？",[evidence('lc-new',0)],"paraphrase")
case("manual-lance","岚册企业版每月多少钱，客服电话是什么？",[],"no_answer")
case("manual-lance","只看岚册1.0历史手册，2.0的E201现在是什么意思？",[],"out_of_scope",scope=['lc-old'])

case("manual-xunji","我是空间管理员，就能读取其他空间文档并创建任何外链吗？",[evidence('xj-access',0),evidence('xj-access',1)],"role")
case("manual-xunji","给外部客户发分享链接要哪些前提，最多几天，客户联系人字段能分享吗？",[evidence('xj-access',1)],"condition")
case("manual-xunji","撤销链接能否把对方之前下载的副本也删掉？",[evidence('xj-access',2)],"paraphrase")
case("manual-xunji","文件不到80MB却上传失败，空间总配额是否也要核对，拆文件能绕过吗？",[evidence('xj-storage',0)],"number")
case("manual-xunji","回收站恢复文档后是否马上能问答，权限与索引分别还要做什么？",[evidence('xj-storage',1),evidence('xj-storage',2)],"multi_evidence")
case("manual-xunji","导入旧XLS或只有空白工作表的XLSX，是否都会生成有内容的检索块？",[evidence('xj-storage',3)],"condition")
case("manual-xunji","通知请求超时没收到响应，可换任务ID重新发给全部人吗？部分成功时怎样处理？",[evidence('xj-jobs',1),evidence('xj-jobs',2)],"multi_evidence")
case("manual-xunji","普通编辑者能查询发送回执吗，找不到查询权限能返回空列表当作没发送吗？",[evidence('xj-jobs',3)],"role")
case("manual-xunji","循迹使用哪家SMTP服务商，每月通知费用多少？",[],"no_answer")
case("manual-xunji","仅搜索访问与分享手册，回收站保留几天和任务回执重试规则是什么？",[],"out_of_scope",scope=['xj-access'])


def save(path,data):
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render(folder):
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,KeepTogether
    pdfmetrics.registerFont(TTFont("CJK","C:/Windows/Fonts/simsun.ttc",subfontIndex=0))
    body=ParagraphStyle("body",fontName="CJK",fontSize=11,leading=18,spaceAfter=12,wordWrap="CJK")
    heading=ParagraphStyle("section",fontName="CJK",fontSize=13,leading=20,spaceBefore=10,spaceAfter=8,wordWrap="CJK")
    title=ParagraphStyle("title",fontName="CJK",fontSize=19,leading=28,spaceAfter=15,wordWrap="CJK")
    note=ParagraphStyle("note",fontName="CJK",fontSize=9,leading=14,textColor=colors.HexColor("#555555"),spaceAfter=14,wordWrap="CJK")
    folder.mkdir(parents=True,exist_ok=False)
    (folder/"pdf").mkdir()
    for doc in DOCS:
        path=folder/"pdf"/(doc["id"]+".pdf")
        story=[Paragraph(escape(doc["title"]),title),Paragraph("原创虚构评测资料 | 仅用于本地检索验证，不对应真实企业或产品",note)]
        for i,(label,text) in enumerate(doc["sections"],1):
            story.extend([Paragraph(f"{i}. {escape(label)}",heading),Paragraph(escape(text),body)])
        if doc["table"]:
            story.append(Paragraph("规则速查表（与正文配套）",heading))
            table=Table([[Paragraph(escape(cell),body) for cell in row] for row in doc["table"]],colWidths=[220,230],repeatRows=1,hAlign="LEFT")
            table.setStyle(TableStyle([("GRID",(0,0),(-1,-1),.4,colors.grey),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#EEEEEE")),("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),8),("RIGHTPADDING",(0,0),(-1,-1),8),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6)]))
            story.append(table)
        def footer(canvas,document):
            canvas.saveState()
            canvas.setFont("CJK",9)
            canvas.drawString(54,32,"虚构评测资料 - 非现实业务依据")
            canvas.drawRightString(540,32,str(document.page))
            canvas.restoreState()
        SimpleDocTemplate(str(path),pagesize=(595,842),rightMargin=54,leftMargin=54,topMargin=48,bottomMargin=54,title=doc["title"],author="Briefly AI 原创评测").build(story,onFirstPage=footer,onLaterPages=footer)
    save(folder/"authored.json",dict(documents=DOCS,cases=CASES,source_sha256=sha(Path(__file__))))
    print(folder,flush=True)


def find_quote(text,quote):
    positions=[i for i,char in enumerate(text) if not char.isspace()]
    normalized="".join(text[i] for i in positions)
    target=re.sub(r"\s+","",quote)
    if normalized.count(target)!=1:
        raise ValueError("解析依据不存在或不唯一，拒绝补写预期")
    at=normalized.index(target)
    return text[positions[at]:positions[at+len(target)-1]+1]


def parse(folder):
    from briefly.loaders import load_document
    (folder/"parsed").mkdir(exist_ok=True)
    docs=[]
    reports=[]
    texts={}
    for doc in DOCS:
        original=folder/"pdf"/(doc["id"]+".pdf")
        loaded=load_document(original.name,original.read_bytes())
        normalized=re.sub(r"\s+","",loaded.text)
        # 全文业务段落保留检查，不只核对问答用到的短句。
        body_checks=[re.sub(r"\s+","",text) in normalized for _,text in doc["sections"]]
        row_checks=[]
        if doc["table"]:
            lines=[re.sub(r"\s+","",line) for line in loaded.text.splitlines()]
            row_checks=[any("".join(row) in line for line in lines) for row in doc["table"]]
        quality="partial_table_relationships" if row_checks and not all(row_checks) else "body_text_checked"
        reports.append(dict(id=doc["id"],report=loaded.report,all_body_preserved=all(body_checks),table_rows_preserved=row_checks,quality=quality,characters=len(loaded.text)))
        if not all(body_checks):
            save(folder/"parse-checks.json",reports)
            raise ValueError("新增PDF正文条件未完整保留，不能进入选型评测")
        path=folder/"parsed"/(doc["id"]+".md")
        path.write_text(loaded.text,encoding="utf-8")
        texts[doc["id"]]=loaded.text
        docs.append({k:doc[k] for k in ("id","family","kb","split","title","authorization")} | dict(
            original_path=original.relative_to(ROOT).as_posix(),original_sha256=sha(original),parsed_path=path.relative_to(ROOT).as_posix(),parsed_sha256=sha(path),characters=len(loaded.text),
            quality=quality,limitations=["只验收正文检索；表格被逐格展开，不能证明行列归属"] if quality.startswith("partial") else []))
    cases=[]
    for case in CASES:
        groups=[[dict(document=ref["document"],quote=find_quote(texts[ref["document"]],ref["authored_quote"]),authored_quote=ref["authored_quote"]) for ref in group] for group in case["evidence_groups"]]
        cases.append({**case,"evidence_groups":groups,"evidence_capability":"body_text_only", "table_relationship_required":False})
    old=json.loads((ROOT/"files/eval/w3/retrieval/strengthening/dataset.json").read_text(encoding="utf-8"))
    for doc in old["documents"]:
        doc.update(family="legacy-office-dev",kb="office",split="dev")
    documents=old["documents"]+docs
    names={"office-chenglan":"澄岚研发采购v2及配套历史/例外资料", "project-bochuan":"泊川知识门户项目",
           "project-shanhe":"杉河客户档案迁移项目", "manual-lance":"岚册工作空间2.0及历史手册", "manual-xunji":"循迹工作台3.2"}
    for i,case in enumerate(cases):
        if case["kind"]=="out_of_scope":
            case["scope_mode"]="explicit_few"
        else:
            case["scope_mode"]="kb" if i%2 else "all"
            case["scope"]=[d["id"] for d in documents if case["scope_mode"]=="all" or d["kb"]==case["category"]]
            case["question"]="关于"+names[case["family"]]+"："+case["question"]
    for case in old["cases"]:
        case.update(family="legacy-office-dev",category="office")
    save(folder/"documents.json",dict(version="w3-selection-v2",documents=documents,
         limitations="10真实办公DOC快照+15原创虚构PDF；不是三类真实企业分布抽样，不覆盖扫描件/OCR/复杂图形。"))
    save(folder/"cases-dev.json",dict(cases=old["cases"]+[c for c in cases if c["split"]=="dev"]))
    save(folder/"cases-validation.json",dict(cases=[c for c in cases if c["split"]=="validation"]))
    save(folder/"parse-checks.json",reports)
    save(folder/"family-split.json",dict(dev=sorted({d['family'] for d in documents if d['split']=='dev'}),validation=sorted({d['family'] for d in documents if d['split']=='validation'}),
         note="按整个家族分开；非把同文档换问法分两组。验证问题文件与开发分开，不进入索引。"))
    print(json.dumps(dict(documents=len(documents),new_pdf=len(docs),dev=35,validation=30,all_body_checks=True,
                         partial_table_documents=sum(r["quality"].startswith("partial") for r in reports))),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode",choices=("render","parse"),required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if args.mode=="render":
        render(args.output.resolve())
    else:
        parse(args.output.resolve())


if __name__=="__main__":
    main()
