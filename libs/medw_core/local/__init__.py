# Local implementations of every port.
#
# These are deliberately trivial. They are not a local *product* - they exist
# so that each Protocol in medw_core.ports has a second implementation, which
# is the only thing that proves the abstraction is not just the Azure SDK
# wearing a different name.
#
# That risk is real and was nearly realised: SparseIndex was almost typed with
# an OData filter string, which no local index could ever have satisfied. You
# find that by writing the second implementation, not by staring at the first.
#
# Selected by MEDW_BACKEND=local. See medw_core.composition.
