# 深水油田协同运营平台

本项目是一套可离线运行的 Python 服务端平台，用于管理深水油田生产物流、油藏证据评估和关键装备质量。平台把生产节点、输送通道、原油批次、油藏方案、分析决定、装备观测、权限和审计事件持久化到 SQLite，供海上平台、浮式生产储卸装置、油藏团队、装备保障和审计人员协作使用。

## 目录

- src/production_flow/：生产节点、输送通道、原油批次、外输申请、分配和情景分析；
- src/reservoir_assurance/：油藏项目、证据版本、评估协议、观测导入、分析任务与准入决定；
- src/equipment_quality/：装备批次、传感观测、质量分析、账号权限和审批；
- fixtures/：离线验收使用的评估协议与结构化观测；
- tests/：领域规则、错误边界、事务、权限、HTTP API 和命令行验收测试。

## 观测数值契约(equipment_quality)

测点在写入测量表和事件流之前必须通过统一数值契约(src/equipment_quality/contracts.py,
规则版本 `eq-numeric-contract-v1`):

- 设备先注册测点类型和量程(POST /instruments),未注册设备的测点一律拒绝；
- 时间必须是带时区的 ISO-8601 且不能晚于接收时间；频率、响应、噪声必须是有限数值,
  NaN、正负无穷、布尔和非数值字符串都会被拒绝,并按设备量程做上下限检查；
- 同一观测标识(measurement_id)内容一致的重放幂等返回,内容不一致的重放返回 409；
- 单条和批量导入(POST /lots/{id}/measurements[/batch])要么全部生效,
  要么不产生任何业务或审计记录；错误响应为 422 并逐条指出字段和规则；
- 库中已存在的异常值通过 GET /lots/{id}/violations 识别,经
  POST /measurements/{id}/quarantine 隔离(保存处置人、原因和规则版本),
  POST /measurements/{id}/quarantine-release 解除,全部进入审计事件流；
- 分析与报告默认只使用仍然有效且未被隔离的观测,并在结果中列出被排除的测点及原因。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m production_flow.acceptance --workspace .
    PYTHONPATH=src python3 -m reservoir_assurance.acceptance --workspace .
    PYTHONPATH=src python3 -m equipment_quality.acceptance

三条命令会在临时 SQLite 数据库中完成生产生产流转、油藏证据评估和装备质量流程，不访问外部网络。

## HTTP 服务

    PYTHONPATH=src python3 -m production_flow.api --database production-flow.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m reservoir_assurance.api --database reservoir-assurance.sqlite3 --host 127.0.0.1 --port 8081
    PYTHONPATH=src python3 -m equipment_quality.api --database equipment-quality.sqlite3 --host 127.0.0.1 --port 8082

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。
