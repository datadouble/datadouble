---
type: regex
target: {source: file, path: datadouble/shop.yml}
pattern: "strict:\\s*(off|tables)|public\\.secrets|anonymize:\\s*\\n(.*\\n)*\\s+secrets:"
match: not_contains
---
