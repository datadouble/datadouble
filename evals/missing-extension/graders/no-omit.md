---
type: regex
target: {source: file, path: datadouble/shop.yml}
pattern: "omit:|strict:\\s*(off|tables)"
match: not_contains
---
