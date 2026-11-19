package litebackup

// protocol.gen.go holds the socket API's schemas, generated from
// ../client-openapi.yaml with names intact. The second command drops the
// generator's package comment, which would replace this package's own.
//go:generate go run github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen@v2.8.2 --config protocol-codegen.yaml ../client-openapi.yaml
//go:generate go run ./internal/stripdoc protocol.gen.go
