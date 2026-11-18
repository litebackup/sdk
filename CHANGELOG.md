# 0.4.0

**Breaking change.** `litebackup backup` now runs once in the foreground and waits for server acknowledgement. Existing scripts that need continuous backup must switch to `litebackup start`. Use `litebackup stop NAME` for a single-file backup, `litebackup stop --rule ID` for an entire backup rule, and `backup PATH --wait cloud` for a scheduled job that must reach the bucket.

TypeScript applications should await `startBackup(db)`. Go applications should call `StartBackup(ctx, path, nil)` for the default destination. Both return after durable local registration. Destination names preserve case and spelling.

After upgrading, use `litebackup rules` and `litebackup status` to record existing source paths, rule IDs, and remote names. Remove each old rule with `litebackup stop --rule ID`, then register it again with `litebackup start`. For a single file, supply its previous remote name explicitly to keep publishing to that history. For a glob or directory, confirm that its prefix and relative filenames still produce the names you want.

Go configuration types now use `Args` and are passed by pointer. For example, pass `nil` for defaults or `&litebackup.StartBackupArgs{Prefix: "tenants/"}` for an explicit prefix. TypeScript keeps its `Options` interfaces.
