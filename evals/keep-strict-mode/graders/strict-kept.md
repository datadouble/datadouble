---
type: regex
target: {source: file, path: datadouble/shop.yml}
pattern: "strict:|public\\.secrets|anonymize:\\s*\\n(.*\\n)*\\s+secrets:"
match: not_contains
---
