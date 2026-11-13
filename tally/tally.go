package tally

import "strings"

/*
Count returns how many items are non-empty.
Whitespace-only items count as empty.
*/
func Count(items []string) int {
	n := 0
	for _, item := range items {
		// Trim before testing.
		if strings.TrimSpace(item) != "" {
			n++
		}
	}
	return n
}
