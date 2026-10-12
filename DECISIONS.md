# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
A unique `employees.emp_code` index prevents duplicate employees. `(department, emp_code)` supports department lookups, while `(joined_on, department)` supports headcount-by-date queries. A unique attendance `(emp_code, date)` index enforces one daily record per employee. Other attendance indexes support date-ordered lists, employee history, status/date filters, punch-in lookup, and late-leaderboard filtering. I did not add a standalone `status` index because the compound status/date index supports status-filtered queries.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
Both requests can find the employee and build the same daily record. MongoDB’s unique `(emp_code, date)` index allows only one insert. It returns `201`; the competing insert raises `DuplicateKeyError`, converted to `409 Conflict`. Race safety relies on the unique index, not a pre-check.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
The leaderboard uses `$rank` on total late minutes, so tied employees share a rank. Filtering `rank <= limit` may return more than `limit` employees if a tie crosses the cutoff. Results are then sorted by employee code.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
The department summary starts with eligible employee documents, looks up monthly attendance, and uses `preserveNullAndEmptyArrays: true`. Employees without logs remain in the pipeline and count toward headcount.

5. **One thing you would change** if this had to serve 100x the data.
I would benchmark representative data, inspect `explain` results, and tune queries and indexes. If needed, I would consider pre-aggregating analytics.
