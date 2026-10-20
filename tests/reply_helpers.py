"""Comparison fixtures with whole-file line totals."""


def file_entry(filename, patch, **fields):
    lines = (patch or "").split("\n")
    return dict(filename=filename, patch=patch,
                additions=sum(line.startswith("+") for line in lines),
                deletions=sum(line.startswith("-") for line in lines), **fields)
