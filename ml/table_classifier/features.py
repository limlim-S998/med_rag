# The feature surface is deliberately tiny: the title, the header stack and
# the footnotes. Not the cell values - those vary per study, and a classifier
# that keys on them will not generalise to the next sponsor's table shells.

def table_to_document(title: str, header_stack: list[str], footnotes: list[str]) -> str:
    return " \n ".join([title, " ".join(header_stack), " ".join(footnotes)])
