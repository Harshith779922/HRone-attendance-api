# Design decisions

1. **Indexes.** I use unique indexes on `employees(emp_code)` and `attendance_logs(emp_code, date)` to prevent duplicates. The latter supports employee/date queries. Attendance indexes `(date, emp_code)` and `(status, date, emp_code)` support date ranges and sorted lists; `(emp_code, punch_in)` finds the latest punch. Employee indexes `(department, emp_code)`, `(joined_on, department)` and `(department, joined_on)` support department lists and headcounts. I would not add a separate status index because the compound index already covers that filter.

2. **Simultaneous punch-ins.** Both requests find the employee and calculate the attendance date. Both try to insert, but the unique index accepts only one. That request gets 201; the duplicate-key error from the other becomes 409.

3. **Ties.** I use `$rank` before applying the limit. Employees tied at the cutoff are all included, so the result can have more rows than the limit. Equal totals are ordered by employee code.

4. **Headcount.** I start with employees who have joined by month-end, then look up their logs. Someone without logs still counts once.

5. **More data.** At 100 times the data, I would keep daily summaries and update them after corrections. This would avoid repeatedly aggregating the same historical logs.
