package tally

// Count returns how many items are non-empty.
func Count(items []string) int {
	n := 0
	for _, item := range items {
		if item != "" {
			n++
		}
	}
	return n
}
