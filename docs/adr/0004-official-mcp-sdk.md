# 0004. 采用官方 MCP SDK，仓库包改名 tooling/

- 状态：已接受
- 日期：2026-11-06
- 相关：`tooling/`，缺陷 G12，方案 T-P1-06

## 背景

1. 仓库根目录曾有 `mcp/` 包自造 JSON-RPC，只实现 tools/list、tools/call、ping，
   无握手、无版本协商、无 Streamable HTTP，无法接入任何 MCP 生态。
2. 本地 `mcp/` 与 PyPI `mcp` 同名，一旦引入官方 SDK 就会被本地目录遮蔽。

## 决策

- 删除自造实现，改用官方 SDK（1.x `FastMCP` / 2.x `MCPServer`，代码内兼容两种）；
- 仓库包改名为 `tooling/`，彻底消除 import 遮蔽；
- 工具 schema 由类型注解 + docstring 自动生成，不再手写；
- 暴露 `tail` 语义之外的传输：Streamable HTTP 挂载 `/mcp`。

## 后果

- 正面：可被 MCP Inspector / 任意客户端发现调用；schema 与实现不再漂移。
- 负面：依赖官方 SDK 的版本演进（已锁定区间并做双版本兼容）。
- 运维：Streamable HTTP 无状态模式便于多副本，但需宿主 lifespan 初始化任务组。

## 备选方案

- 保留自造协议 + 增加 HTTP 层：等于重写一个 MCP SDK，否决。
