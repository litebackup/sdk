package litebackup

import (
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// sdkNames is testdata/contract/names/sdk-names.json: the name of each
// operation and type in all three SDKs.
type sdkNames struct {
	Operations []struct {
		Operation string `json:"operation"`
		Go        string `json:"go"`
		// GoFunction names a package function rather than a Client method.
		GoFunction bool `json:"go_function"`
	} `json:"operations"`
	Types []string `json:"types"`
}

// readSDKNames reads the cross-SDK name fixture.
func readSDKNames(t *testing.T) *sdkNames {
	t.Helper()
	text, err := os.ReadFile(filepath.Join(contractRoot, "names", "sdk-names.json"))
	if err != nil {
		t.Fatal(err)
	}
	var names sdkNames
	if err := json.Unmarshal(text, &names); err != nil {
		t.Fatal(err)
	}
	return &names
}

// TestSDKNames checks the Go SDK against the names all three SDKs share:
// every listed operation is a Client method, every listed type is exported,
// and every operation a contract fixture runs is listed.
func TestSDKNames(t *testing.T) {
	names := readSDKNames(t)
	clientType := reflect.TypeFor[*Client]()
	declared := exportedDeclarations(t)
	listed := map[string]bool{}
	for _, operation := range names.Operations {
		listed[operation.Operation] = true
		if operation.GoFunction {
			if !declared.functions[operation.Go] {
				t.Errorf("%s: the package has no function %s", operation.Operation, operation.Go)
			}
			continue
		}
		if _, found := clientType.MethodByName(operation.Go); !found {
			t.Errorf("%s: Client has no method %s", operation.Operation, operation.Go)
		}
	}
	for _, typeName := range names.Types {
		if !declared.types[typeName] {
			t.Errorf("the package exports no type %s", typeName)
		}
	}
	fixturePaths, err := filepath.Glob(filepath.Join(contractRoot, "*", "*.json"))
	if err != nil {
		t.Fatal(err)
	}
	for _, fixturePath := range fixturePaths {
		text, err := os.ReadFile(fixturePath)
		if err != nil {
			t.Fatal(err)
		}
		var fixture struct {
			Operation string `json:"operation"`
		}
		// Fixtures that are not one JSON object, and fixtures without an
		// operation, run no operation.
		if json.Unmarshal(text, &fixture) != nil || fixture.Operation == "" {
			continue
		}
		if !listed[fixture.Operation] {
			t.Errorf(
				"%s runs %s, which sdk-names.json does not list",
				filepath.Base(fixturePath),
				fixture.Operation,
			)
		}
	}
}

// packageDeclarations are the exported names a package declares.
type packageDeclarations struct {
	types     map[string]bool
	functions map[string]bool
}

// exportedDeclarations lists the exported types and package functions the
// package declares.
func exportedDeclarations(t *testing.T) *packageDeclarations {
	t.Helper()
	files := token.NewFileSet()
	packages, err := parser.ParseDir(files, ".", func(file os.FileInfo) bool {
		return !strings.HasSuffix(file.Name(), "_test.go")
	}, 0)
	if err != nil {
		t.Fatal(err)
	}
	declared := &packageDeclarations{types: map[string]bool{}, functions: map[string]bool{}}
	for _, sdkPackage := range packages {
		for _, file := range sdkPackage.Files {
			for _, declaration := range file.Decls {
				function, isFunction := declaration.(*ast.FuncDecl)
				if isFunction && function.Recv == nil && function.Name.IsExported() {
					declared.functions[function.Name.Name] = true
				}
				general, isGeneral := declaration.(*ast.GenDecl)
				if !isGeneral || general.Tok != token.TYPE {
					continue
				}
				for _, spec := range general.Specs {
					typeSpec := spec.(*ast.TypeSpec)
					if typeSpec.Name.IsExported() {
						declared.types[typeSpec.Name.Name] = true
					}
				}
			}
		}
	}
	return declared
}
