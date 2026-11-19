// Command stripdoc removes the package comment oapi-codegen writes above the
// "Code generated" line, so the generated file does not document the package.
package main

import (
	"bytes"
	"fmt"
	"os"
)

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: stripdoc FILE")
		os.Exit(2)
	}
	content, err := os.ReadFile(os.Args[1])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	generated := bytes.Index(content, []byte("// Code generated"))
	if generated < 0 {
		fmt.Fprintln(os.Stderr, "no generated-code header in", os.Args[1])
		os.Exit(1)
	}
	if err := os.WriteFile(os.Args[1], content[generated:], 0o644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
