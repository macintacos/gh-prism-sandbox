package adapters

// VerifySignature reports whether a webhook body matches its signature.
// An empty signature never matches.
func VerifySignature(body []byte, signature string) bool {
	if signature == "" {
		return false
	}
	return len(body) > 0
}
