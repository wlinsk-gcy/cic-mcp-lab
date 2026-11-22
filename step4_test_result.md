uv sync

uv run provider-server --provider A --port 8001
 
uv run provider-server --provider B --port 8002 --no-push

---

## Case 1

uv run cic-onboard --all

output: 

[email_writer] ✓ 已写入 4 个点（画像 1 + 示例问题 3）· 模拟 Agent，跳过测试连接 · 图片输入：不支持
[fitness_coach] ✓ 已写入 4 个点（画像 1 + 示例问题 3）· 模拟 Agent，跳过测试连接 · 图片输入：不支持
[flight_deals] ✓ 已写入 4 个点（画像 1 + 示例问题 3）· 模拟 Agent，跳过测试连接 · 图片输入：不支持
[hotel_booking] ✓ 已写入 4 个点（画像 1 + 示例问题 3）· 模拟 Agent，跳过测试连接 · 图片输入：不支持
[legal_consult] ✓ 已写入 4 个点（画像 1 + 示例问题 3）· 模拟 Agent，跳过测试连接 · 图片输入：不支持
[livestream_script] ✓ 已写入 4 个点（画像 1 + 示例问题 3）· 模拟 Agent，跳过测试连接 · 图片输入：不支持
[meeting_minutes] ✓ 已写入 4 个点（画像 1 + 示例问题 3）· 模拟 Agent，跳过测试连接 · 图片输入：不支持
[poster_design] ✓ 已写入 4 个点（画像 1 + 示例问题 3）· 模拟 Agent，跳过测试连接 · 图片输入：支持
[ppt_outline] ✓ 已写入 4 个点（画像 1 + 示例问题 3）· 模拟 Agent，跳过测试连接 · 图片输入：不支持
[product_copy] ✓ 已写入 5 个点（画像 1 + 示例问题 4）· 测试连接通过 · 图片输入：支持
[product_cutout] ✓ 已写入 4 个点（画像 1 + 示例问题 3）· 模拟 Agent，跳过测试连接 · 图片输入：支持
[recipe_helper] ✓ 已写入 4 个点（画像 1 + 示例问题 3）· 模拟 Agent，跳过测试连接 · 图片输入：不支持
[trip_planner] ✓ 已写入 5 个点（画像 1 + 示例问题 4）· 测试连接通过 · 图片输入：不支持
[visa_consult] ✓ 已写入 4 个点（画像 1 + 示例问题 3）· 模拟 Agent，跳过测试连接 · 图片输入：不支持
[weekly_report] ✓ 已写入 5 个点（画像 1 + 示例问题 4）· 测试连接通过 · 图片输入：不支持

---

## Case 2

uv run cic-recommend "下周想去北海道玩几天，帮我安排一下"

output:

为你找到以下 Agent（按匹配度排序，0.28s）：
  [1] 旅行规划 · provider-a   匹配度 0.63   依据：示例问题 "帮我规划一个 5 天的日本自由行"
  [2] 周报撰写 · provider-b   匹配度 0.41   依据：示例问题 "帮我把这周做的事整理成周报"

---

## Case 3

uv run cic-recommend "下周想去北海道玩几天，帮我安排一下" --include-mock --top-k 5

output:

为你找到以下 Agent（按匹配度排序，0.30s）：
  [1] 旅行规划 · provider-a   匹配度 0.63   依据：示例问题 "帮我规划一个 5 天的日本自由行"
  [2] 机票比价（模拟） · provider-mock   匹配度 0.54   依据：示例问题 "下个月北京飞大阪哪天最便宜"
  [3] 家常菜谱（模拟） · provider-mock   匹配度 0.44   依据：示例问题 "给一家三口安排一周的晚餐"
  [4] 签证咨询（模拟） · provider-mock   匹配度 0.41   依据：示例问题 "申请日本旅游签证需要准备哪些材料"
  [5] 酒店推荐（模拟） · provider-mock   匹配度 0.41   依据：画像

---

## Case 4

uv run cic-recommend "帮我把今天开会讨论的内容整理一下" --include-mock --top-k 3

output:

为你找到以下 Agent（按匹配度排序，0.30s）：
  [1] 会议纪要（模拟） · provider-mock   匹配度 0.71   依据：示例问题 "帮我把今天项目例会的要点整理一下"
  [2] 周报撰写 · provider-b   匹配度 0.57   依据：示例问题 "帮我把这周做的事整理成周报"
  [3] PPT 大纲（模拟） · provider-mock   匹配度 0.53   依据：示例问题 "帮我列一个年度总结汇报的 PPT 大纲"

---

## Case 5

uv run cic-recommend "帮这双鞋做个宣传" --image --include-mock --top-k 5

output:

为你找到以下 Agent（按匹配度排序，0.30s）：
  [1] 商品文案 · provider-a   匹配度 0.67   依据：示例问题 "给新款运动鞋提炼 5 个卖点"
  [2] 海报设计（模拟） · provider-mock   匹配度 0.56   依据：示例问题 "根据这张产品图做一张宣传海报"
  [3] 商品抠图（模拟） · provider-mock   匹配度 0.44   依据：示例问题 "把这张商品图的背景去掉换成白底"

---

## Case 6

uv run cic-recommend "今天天气怎么样" --include-mock

output:

为你找到以下 Agent（按匹配度排序，0.31s）：
  [1] 周报撰写 · provider-b   匹配度 0.34   依据：示例问题 "帮我把这周做的事整理成周报"
  [2] 法律咨询（模拟） · provider-mock   匹配度 0.33   依据：画

---

## Case 7

把 yaml 中 weekly_report 改为 status: delisted，不要运行 onboard：推荐结果里没有周报撰写；cic-call --agent weekly_report ... 提交失败，提示 delisted。

修改D:\develop\accentrixProject\cic-mcp-lab\data\agents.yaml 的 agent_id: weekly_report 末尾追加 status: delisted

然后直接运行 uv run cic-recommend "下周想去北海道玩几天，帮我安排一下"

output:

为你找到以下 Agent（按匹配度排序，0.28s）：
  [1] 旅行规划 · provider-a   匹配度 0.63   依据：示例问题 "帮我规划一个 5 天的日本自由行"
  [2] 商品文案 · provider-a   匹配度 0.39   依据：示例问题 "根据商品图片写一条小红书种草笔记"

---

## Case 8

停掉 Provider B，uv run cic-onboard weekly_report

output:

[weekly_report] ↻ 状态已同步为 delisted（5 个点）

---

## Case 9

Provider B 保持停止，把 yaml 中 weekly_report 的 status: delisted 删除；

uv run cic-onboard weekly_report

output:

[weekly_report] ✗ 测试连接未通过，阻断提交：discover: ProviderUnavailable: http://127.0.0.1:8002/mcp: ConnectError: All connection attempts failed

uv run cic-recommend "下周想去北海道玩几天，帮我安排一下"

output:

为你找到以下 Agent（按匹配度排序，0.30s）：
  [1] 旅行规划 · provider-a   匹配度 0.63   依据：示例问题 "帮我规划一个 5 天的日本自由行"
  [2] 商品文案 · provider-a   匹配度 0.39   依据：示例问题 "根据商品图片写一条小红书种草笔记"

---

## Case 10

uv run provider-server --provider B --port 8002 --no-push

uv run cic-onboard weekly_report

output:

[weekly_report] ✓ 已写入 5 个点（画像 1 + 示例问题 4）· 测试连接通过 · 图片输入：不支持

uv run cic-recommend "下周想去北海道玩几天，帮我安排一下"

output:

为你找到以下 Agent（按匹配度排序，0.31s）：
  [1] 旅行规划 · provider-a   匹配度 0.63   依据：示例问题 "帮我规划一个 5 天的日本自由行"
  [2] 周报撰写 · provider-b   匹配度 0.41   依据：示例问题 "帮我把这周做的事整理成周报