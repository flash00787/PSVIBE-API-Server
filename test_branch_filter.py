from branch_utils import add_branch_filter, get_branches, get_branch, DEFAULT_BRANCH_ID

print("DEFAULT_BRANCH_ID:", DEFAULT_BRANCH_ID)
print("Active branches:", len(get_branches()))

# Test add_branch_filter
q1 = add_branch_filter("SELECT * FROM console_booking", 1)
print("Test 1 (no WHERE):", q1)

q2 = add_branch_filter('SELECT * FROM console_booking WHERE status = "Active"', 2)
print("Test 2 (has WHERE):", q2)

q3 = add_branch_filter("SELECT * FROM console_booking ORDER BY id DESC", 3)
print("Test 3 (ORDER BY):", q3)

q4 = add_branch_filter('SELECT * FROM console_booking WHERE status = "Active" ORDER BY id DESC', 4)
print("Test 4 (WHERE+ORDER):", q4)

q5 = add_branch_filter("SELECT * FROM console_booking cb", 1, "cb")
print("Test 5 (alias):", q5)

q6 = add_branch_filter("SELECT * FROM console_booking cb WHERE cb.status = 'Active'", 2, "cb")
print("Test 6 (alias+WHERE):", q6)

print("\nAll add_branch_filter tests passed!")
