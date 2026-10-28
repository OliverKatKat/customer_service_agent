# 物流 MCP 第一版

提供 `get_logistics_status(order_id)` 一个只读工具。客户对话的业务编排、退款审批和执行继续由现有 Agent 负责；MCP 不执行退款、拦截或改地址。

## 启动

沿用 `scripts/run_workbench_demo.py`。启动器迁移独立演示库、填入4个虚构包裹与8条轨迹，并授予 wb_logistics_reader 仅物流表的 SELECT 权限。重复初始化不重置人工修改。当前没有真实物流 API。

演示客户 demo_echo_alice，密码见原演示说明。选择订单 DEMO-ECHO-002，提问“订单已经发货，现在想退款，请先核实两个包裹的物流情况”。如需继续已有案件，显式关联 CASE-DEMO-SHIPPED。模型需要先查订单，再调用物流工具和政策工具。两个包裹分别为运输中、已签收，不能据此把整单说成已签收。物流更新时间来自 last_event_at，queried_at 只是查询时间。轨迹均标注 source=synthetic，回答应说明是演示物流。

## 数据与边界

- logistics.shipments 记录租户、客户、订单、包裹、承运商、运单号、来源。一单支持多个包裹。
- logistics.shipment_events 记录事件编号、状态、描述、带时区的发生时间。
- 工具层从可信 RunnableConfig 取租户、客户和工单，重新核对活跃账号、商家及绑定订单。模型只提供订单号。
- 客户端按次启动私有 stdio 子进程，注入这一次已核验的租户、客户和订单范围；不向子进程传主库写连接、JWT 密钥和模型密钥。
- Provider 只暴露一个工具，固定范围并执行参数化 SQL；使用独立账号、只读事务、2秒 SQL 超时。拒绝超级账号和可读主业务订单表的连接。没有配置时不回退主库。
- 这套授权边界依赖受信任的本机 worker。没有开放 HTTP MCP 服务。以后开放给外部客户端时需另加服务端认证和授权，不能直接公开此 stdio 进程。
- 每次查询建立并关闭一个 MCP 会话，含真实工具发现与调用，12秒整体查询限时。Windows 下子进程通信运行在独立线程的 Proactor 循环，避免影响现有数据库 Selector 循环。
- 每单最多返回10个包裹、每包裹20条轨迹，提供截断标记。查询缺失、超时或配置错误只返回未找到/不可用，不能生成物流事实。
- Chat 保留后台实际返回的处理步骤，可展开“本次处理记录”查看查询过程；不展示模型内部思维。

## 代码阅读顺序

1. demo/logistics.json：固定样本。
2. migrations/versions/d34b9c2e7810_logistics.py：两张 PostgreSQL 表。
3. app/mcp_servers/logistics.py：MCP Server 和 SQL。
4. app/integrations/logistics.py：客户端会话与子进程。
5. app/agent/logistics_tools.py：业务鉴权。
6. app/agent/tool_registry.py 与 app/agent/skills/registry.py：模型工具及退款白名单。

验证通过 `scripts/run_workbench_tests.py`，仅在独立55458测试库执行，包含真实 MCP 子进程、SQL归属隔离、只读权限、失败回退、退款工具注册及种子幂等检查。验证不会自动调用外部模型；真实模型选择工具和回答质量须单独观察。
