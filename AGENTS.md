# SDK conventions

Go APIs group arguments in an operation-specific `Args` struct passed by pointer. Use `nil` for defaults and a pointer literal for explicit settings. Validate required fields, such as a restore destination, before starting work. Copy configuration retained after the call.

TypeScript APIs use `Options` interfaces. Allow callers to omit the options object when all its fields are optional. Keep library-owned and generated protocol names intact.
