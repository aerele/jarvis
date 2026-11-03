# Cycle Count source capacity

The Cycle-Count Planner collects one complete, permission-filtered source
snapshot before it sends a private assessment request. It does not send a
partial snapshot for a complete plan.

## Paging

The server reads each supported source with name-keyset paging. It requests at
most 501 rows and adds at most 500 rows to the snapshot before it requests the
next page. The `500` value is a transport page size. It is not a limit on the
number of Bin, Item, Stock Ledger Entry, Stock Entry, Physical Count, or
Physical Count Item rows in a complete source.

## One-file boundary

The complete source must fit in one sealed private input file. The file limit is
8 MiB. Source collection also has a 25-second limit. The server checks the
exact private envelope before it stores it, including server capture metadata.

If a page, cursor, query, time, or byte check fails, the server stores only a
small incomplete result. It removes source rows and source totals. The result
cannot stage a count plan.

Use a smaller warehouse scope when the source does not fit. A shorter movement
or history range can reduce movement and physical-count rows. It cannot reduce
the number of Bin rows in a selected leaf warehouse. A multi-file assessment is
not part of this contract.
