
# DECISIONS.md

## 1. Indexes and rejected index

I created a unique index on `employees.emp_code` because this is the business identifier used by the API and duplicate employee codes must not be allowed. I also created a unique compound index on `attendance_logs.emp_code` and `date` to enforce one attendance record per employee per attendance day, including when requests arrive simultaneously. Additional indexes support department and joining-date filters, attendance date queries, and employee/date-based attendance queries. I did not add a standalone index on `late_minutes`: the leaderboard aggregates late minutes over a month, so a standalone index on that field is not necessarily useful for its main access pattern. The actual benefit of each index should be confirmed with MongoDB execution statistics on representative data.

## 2. Simultaneous identical punch-ins

A check followed by an insert is not enough to prevent duplicates because two requests can pass the check before either inserts a document. The unique compound index on `emp_code` and `date` is the database-level safeguard. If concurrent requests try to create the same attendance record, MongoDB allows only one insert. The losing request handles the duplicate-key error and returns HTTP 409 instead of creating a second record.

## 3. Leaderboard ties at the limit

The leaderboard uses standard competition ranking, so employees with equal total late minutes share the same rank and the next rank is skipped. The limit applies to the rank, not the number of rows returned. Therefore, if multiple employees tie at the cutoff rank, all of them are included even if the response contains more rows than the requested limit. Results are ordered by total late minutes descending and employee code ascending.

## 4. Employees with no attendance logs

The department summary starts with employees and looks up their attendance records. Preserving employees with no matching attendance records ensures they still contribute to headcount. Their missing attendance activity must not be treated as a presence record. Employees who have not yet joined by the reporting month are excluded from that month's headcount.

## 5. Scaling to 100 times the data

I would first inspect execution plans and execution statistics using representative data to identify the slowest queries and aggregations. I would then tune indexes and aggregation stages based on measured query patterns rather than adding indexes indiscriminately. If a single MongoDB deployment remained a bottleneck, I would evaluate workload separation, caching or precomputed summaries where appropriate, and sharding only after measuring the bottleneck and choosing a shard key that matches the access patterns.
