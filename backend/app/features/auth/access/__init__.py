"""What a caller may do: the permission catalogue, the roles that group it, and the
dependencies that make a route demand it.

Separated from the rest of `auth` because these three answer a different question from the
others. `service.py` and `repository.py` answer "who is this"; these answer "and what are
they allowed to do", which is the question every router in the product asks.
"""
