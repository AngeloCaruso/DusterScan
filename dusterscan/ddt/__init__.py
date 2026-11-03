"""Renault module definitions (DDT4All's database format), used read-only.

The user imports their own copy of the database once (store.py). Each module
the scan finds is matched to a definition by its identification (match.py);
the definition then says what the module's fault codes mean and how to read
and decode its values (decode.py, reader.py). Only read requests are ever sent,
and every one still passes safety.check_command.
"""
