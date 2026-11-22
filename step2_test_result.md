mcp server 启动命令：

uv run provider-server --provider A --port 8001

uv run provider-server --provider B --port 8002 --no-push

---

check：

uv run provider-check --port 8001 --key-env PROVIDER_A_API_KEY --agent trip_planner --message "帮我规划旅行"

output: 

[auth] 不带 key：HTTP 401
[discover] 声明 tasks 扩展：True
[tools] ['trip_planner', 'product_copy']
[call] 0.01s 返回 resultType=task
[task] taskId=d77da285a7434ecc8ad39a8e111baa83 pollIntervalMs=2000 ttlMs=3600000
[listen] 订阅确认 taskIds=['d77da285a7434ecc8ad39a8e111baa83']
[push] 收到任务结束推送
[task] status=completed，从提交起共 2.5s
[task] tasks/get 与推送/轮询结果一致：True
{
  "conversation_id": "conv_3a48d5bb7378496980aba1f05b466ee0",
  "status": "input_required",
  "reply": "好的！开始规划前，请告诉我三个信息：\n1. 想去哪个目的地？\n2. 出行日期或计划玩几天？\n3. 总预算大概多少（含交通、住宿、餐饮等）？"
}
[credit] com.cic/credit_usage=704

---

uv run provider-check --port 8001 --key-env PROVIDER_A_API_KEY --agent trip_planner --conversation-id conv_3a48d5bb7378496980aba1f05b466ee0 --message "去广州，明天出发玩2天，预算2000"

output:

[auth] 不带 key：HTTP 401
[discover] 声明 tasks 扩展：True
[tools] ['trip_planner', 'product_copy']
[call] 0.00s 返回 resultType=task
[task] taskId=6f6d3041d25746bb9100e8e28c69015b pollIntervalMs=2000 ttlMs=3600000
[listen] 订阅确认 taskIds=['6f6d3041d25746bb9100e8e28c69015b']
[push] 收到任务结束推送
[task] status=completed，从提交起共 11.4s
[task] tasks/get 与推送/轮询结果一致：True
{
  "conversation_id": "conv_3a48d5bb7378496980aba1f05b466ee0",
  "status": "completed",
  "reply": "已为你规划好广州2日游行程（11月23日—11月24日出发，总预算2000元）：\n\n【第1天 · 11月23日（周一）：老城风情】\n- 上午：抵达广州后前往陈家祠（门票约10元），欣赏岭南建筑与雕刻艺术；随后步行至上下九步行街，感受西关骑楼老街。\n- 下午：逛永庆坊、荔枝湾涌，打卡粤剧艺术博物馆（免费）；在泮溪酒家或街边老
店品尝虾饺、肠粉、双皮奶等广式点心。\n- 晚上：沙面岛散步看欧式建筑夜景，之后到珠江边天字码头乘船夜游珠江（船票约80—100元），欣赏广州塔灯光。\n\n【第2天 · 11月24日（周二）：都市新貌】\n- 上午：越秀公园登镇海楼、看五羊石像（公园免费，镇海楼约10元）；随后到北京路步行街逛街、吃午餐（推荐煲仔饭、云吞面）。\n- 下
午：前往花城广场、广东省博物馆（免费，需提前预约）；在海心沙一带拍照打卡。\n- 晚上：登广州塔观景（门票约150元，可选450米观景层），俯瞰珠江新城夜景后返程。\n\n【预算分配（共2000元）】\n- 往返大交通：约600元（视出发地调整，高铁/机票）\n- 住宿1晚：约350元（北京路或体育西路附近舒适型酒店）\n- 餐饮2天：约400元（
早茶、点心、正餐）\n- 市内交通：约100元（地铁为主，可购地铁日票）\n- 门票及夜游：约300元（陈家祠、镇海楼、珠江夜游、广州塔）\n- 机动备用：约250元（购物、打车或临时开销）\n\n小贴士：\n1. 广东省博物馆需在官方公众号提前预约，周一部分场馆闭馆，省博安排在周二正合适。\n2. 11月下旬广州气温约15—23℃，建议带一件薄外 套。\n3. 若大交通费用超出600元，可压缩广州塔登塔项目（改为塔下拍照）来平衡预算。\n\n祝旅途愉快！如需调整节奏或替换景点，随时告诉我。"
}
[credit] com.cic/credit_usage=1375

---

uv run provider-check --port 8002 --key-env PROVIDER_A_API_KEY --agent weekly_report --message "写周报"

output:

[auth] 不带 key：HTTP 401
[auth] key 被拒绝：HTTP 401

---

uv run provider-check --port 8002 --key-env PROVIDER_B_API_KEY --agent weekly_report --message "写周报"

output:

[auth] 不带 key：HTTP 401
[discover] 声明 tasks 扩展：True
[tools] ['weekly_report']
[call] 0.00s 返回 resultType=task
[task] taskId=f7186551cdcf4f708d0b17633f8aaa5b pollIntervalMs=2000 ttlMs=3600000
[listen] 订阅确认 taskIds=[]
[poll] 未通过推送拿到结果，改为轮询 tasks/get
[task] status=completed，从提交起共 4.1s
[task] tasks/get 与推送/轮询结果一致：True
{
  "conversation_id": "conv_db99bd16bac84611a8ec21e69eae4b90",
  "status": "input_required",
  "reply": "请问你本周完成了哪些主要工作？可以简单列几条，我来帮你整理成周报。"
}
[credit] com.cic/credit_usage=675

---

mcp 服务启动命令：

uv run provider-server --provider A --port 8001 --delay 90

uv run provider-check --port 8001 --key-env PROVIDER_A_API_KEY --agent trip_planner --message "帮我规划旅行"

output:

[auth] 不带 key：HTTP 401
[discover] 声明 tasks 扩展：True
[tools] ['trip_planner', 'product_copy']
[call] 0.00s 返回 resultType=task
[task] taskId=c42df93868584599aec01732f89aa07c pollIntervalMs=2000 ttlMs=3600000
[listen] 订阅确认 taskIds=['c42df93868584599aec01732f89aa07c']
[push] 收到任务结束推送
[task] status=completed，从提交起共 92.2s
[task] tasks/get 与推送/轮询结果一致：True
{
  "conversation_id": "conv_1e04132f6d6c47e6b875edbbf9295e1c",
  "status": "input_required",
  "reply": "好的！开始规划前，请先告诉我三个信息：1）想去哪个目的地？2）出行日期或计划玩几天？3）总预算大概多少？"
}
[credit] com.cic/credit_usage=689

