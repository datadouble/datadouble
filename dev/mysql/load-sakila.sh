#!/bin/sh
# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Bulk-load the Sakila fixture into the schema the previous script created.
set -eu
client() {
    mysql --protocol=socket -uroot -p"$MYSQL_ROOT_PASSWORD" --local-infile=1 "$@"
}
client -e "SET GLOBAL local_infile = 1"
while IFS="$(printf '\t')" read -r table _rows; do
    columns=$(head -n 1 "/fixtures/sakila/$table.tsv" | tr '\t' ',')
    client sakila -e "SET foreign_key_checks = 0;
        LOAD DATA LOCAL INFILE '/fixtures/sakila/$table.tsv' INTO TABLE $table
        CHARACTER SET utf8mb4 IGNORE 1 LINES ($columns)"
done < /fixtures/sakila/tables.txt
client -e "SET GLOBAL local_infile = 0"
