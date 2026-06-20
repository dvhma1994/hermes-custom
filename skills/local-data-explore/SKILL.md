---
name: local-data-explore
description: Load and query local data with sqlite and excel MCP servers.
version: 1.0.0
author: Hermes Agent
metadata:
  hermes:
    tags: [data, sqlite, excel, exploration, local]
    category: data-science
---

# Local Data Exploration

Explore structured data entirely on your local machine using two bundled MCP
servers — `sqlite` and `excel`. No external service, no API key, no network
egress: everything runs against local `.db`/`.sqlite` and `.xlsx` files that the
MCP servers already have configured.

## When to Use

- You have a local SQLite database or Excel workbook and need to answer a
  question about its contents (row counts, grouped sums, joins, lookups).
- You need to move data between Excel and SQLite — e.g., push a sheet into a SQL
  table to join it, or export a query result back into a formatted spreadsheet.
- You want quick ad-hoc analysis without spinning up a notebook or writing a
  script. The MCP tools are the fastest path from "where is the data?" to "here
  is the answer."
- Privacy/sandbox constraints forbid calling external APIs or uploading data.

## Prerequisites

Two MCP servers must be available in the current agent session:

- `sqlite` MCP server — local SQLite database. Tools:
  - `mcp_sqlite_list_tables` — list all tables in the attached database.
  - `mcp_sqlite_describe_table` — show columns, types, and nullability for one
    table (pass `table_name`).
  - `mcp_sqlite_read_query` — run a `SELECT` query; returns rows as JSON.
  - `mcp_sqlite_write_query` — run `INSERT`/`UPDATE`/`DELETE` (mutates the DB).
  - `mcp_sqlite_create_table` — run a `CREATE TABLE` statement.
- `excel` MCP server — local `.xlsx` files by path. Tools:
  - `mcp_excel_get_workbook_metadata` — list sheets and used ranges in a
    workbook (pass `filepath`).
  - `mcp_excel_read_data_from_excel` — read a sheet range; returns cell-by-cell
    values + validation metadata (pass `filepath`, `sheet_name`, optional
    `start_cell`/`end_cell`).
  - `mcp_excel_write_data_to_excel` — write a 2-D list of rows starting at a
    cell.
  - `mcp_excel_apply_formula` — write a formula string into a single cell.
  - `mcp_excel_create_pivot_table` — build a pivot table from a data range.

Both servers are local; no external service or API key is required.

## Procedure

1. **Identify the file type.** A `.db`/`.sqlite`/`.sqlite3` file is SQLite; a
   `.xlsx` is Excel. If the path is unknown, ask the user or `list_dir` first.
2. **For SQLite — inspect, then query.**
   - `mcp_sqlite_list_tables` → see what tables exist.
   - `mcp_sqlite_describe_table` with `table_name: "orders"` → learn the schema
     before writing SQL blind.
   - `mcp_sqlite_read_query` with `query: "SELECT region, COUNT(*) AS n, SUM(total) AS s FROM orders GROUP BY region ORDER BY s DESC;"`.
   - Never run `mcp_sqlite_write_query` (INSERT/UPDATE/DELETE/DROP) on a first
     pass — only after confirming the user wants a mutation.
3. **For Excel — metadata first, then targeted reads.**
   - `mcp_excel_get_workbook_metadata` with `filepath: "C:/Users/me/sales.xlsx"`
     → discover sheet names and ranges.
   - `mcp_excel_read_data_from_excel` with `filepath`, `sheet_name: "Q1"`, and
     a bounded range like `start_cell: "A1"`, `end_cell: "F50"` — avoid reading
     entire huge sheets.
4. **Move data between the two** when one is better suited to the task:
   - Excel → SQLite for joins/aggregation: read the sheet range, then
     `mcp_sqlite_create_table` to make a matching table and
     `mcp_sqlite_write_query` (or repeated inserts) to load the rows.
   - SQLite → Excel for reporting: `mcp_sqlite_read_query` to get rows, then
     `mcp_excel_write_data_to_excel` to drop them into a fresh sheet starting at
     `A1`, optionally `mcp_excel_apply_formula` for totals.
5. **Summarize and aggregate.** Prefer SQL `GROUP BY` for grouped sums/counts
   when the data is in SQLite; use `mcp_excel_create_pivot_table` when the data
   is better consumed in a spreadsheet. Always back the final answer with a
   concrete query or cell-range result.

## Quick Reference

```
# SQLite
mcp_sqlite_list_tables
mcp_sqlite_describe_table       { table_name }
mcp_sqlite_read_query           { query: "SELECT ..." }
mcp_sqlite_write_query          { query: "INSERT|UPDATE|DELETE ..." }   # MUTATES
mcp_sqlite_create_table         { query: "CREATE TABLE ..." }          # MUTATES

# Excel
mcp_excel_get_workbook_metadata { filepath }
mcp_excel_read_data_from_excel  { filepath, sheet_name, start_cell?, end_cell? }
mcp_excel_write_data_to_excel   { filepath, sheet_name, data: [[...]], start_cell? }
mcp_excel_apply_formula         { filepath, sheet_name, cell, formula }
mcp_excel_create_pivot_table    { filepath, sheet_name, data_range, rows, values, agg_func? }
```

## Pitfalls

- **`mcp_sqlite_write_query` mutates the database.** Confirm with the user
  before any INSERT/UPDATE/DELETE/DROP/CREATE. On a first exploration pass, use
  only `mcp_sqlite_read_query` with `SELECT`.
- **Excel cell coordinates are 1-indexed letters + numbers** (`A1`, `B12`), not
  zero-indexed row/col integers. Off-by-one reads are the most common mistake.
- **Blank Excel cells read as `null`, not `0` or `""`.** Aggregations like
  `SUM` in SQL will ignore nulls; arithmetic in Python may blow up — coerce
  explicitly.
- **Large sheets — read a bounded range, not the whole sheet.** Pass
  `start_cell`/`end_cell` to `mcp_excel_read_data_from_excel` instead of letting
  it expand to the used range, or you may pull tens of thousands of rows.
- **Schema differs per database.** Never assume column names; always
  `mcp_sqlite_describe_table` first. Column order and casing are not
  guaranteed.
- **SQLite stores a single type affinity per column**, not strict types — a
  "TEXT" column may still hold numbers. Cast in SQL when needed
  (`CAST(col AS REAL)`).

## Verification

You are done when you can answer a concrete question about the data backed by an
actual tool result, for example:

- Row count: `mcp_sqlite_read_query` with
  `SELECT COUNT(*) FROM orders WHERE status = 'shipped';` → report the number.
- Grouped sum: `SELECT region, SUM(total) FROM orders GROUP BY region;` →
  present the top region by revenue.
- Excel lookup: `mcp_excel_read_data_from_excel` over a bounded range, then
  report a specific cell value or a small derived metric.

Always cite the query/range you ran and the value returned. If a question cannot
be answered with a single tool call, chain them (metadata → schema → query) and
show the chain. Do not fabricate numbers — if a query fails, surface the error
and adjust the SQL or range.
