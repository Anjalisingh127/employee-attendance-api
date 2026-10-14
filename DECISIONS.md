
# DECISIONS.md

## 1. Indexes

A unique index on `employees.emp_code` prevents duplicate employee identifiers. A unique compound index on `attendance_logs.emp_code` and `date` enforces one attendance record per employee per day. Additional indexes support department/joining-date filters and attendance date and employee/date queries. Index usefulness should be assessed against actual query plans and measured workloads.

## 2. Simultaneous identical punch-ins

A check followed by an insert is not concurrency-safe. The unique compound index allows only one matching insert. The other request receives a duplicate-key error, which the API translates to HTTP 409.

## 3. Leaderboard ties at the limit

Tied employees share a rank, and the next rank is skipped. The limit applies to ranks, so every employee tied at the cutoff is included. Ties are ordered by employee code.

## 4. Employees without attendance logs

Department summaries start from employees and look up attendance records, preserving employees with no logs in headcount. Employees who joined after the reporting month are excluded.

## 5. Scaling

I would inspect execution statistics on representative data, then tune indexes and aggregation stages based on measured bottlenecks. If necessary, I would evaluate caching, precomputed summaries, workload separation, and sharding after measuring the workload and selecting an appropriate shard key.
