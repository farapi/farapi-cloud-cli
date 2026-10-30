"""The farapi command line: a thin client of the farapi-ops tool catalog (spec 17).

Every command except the built-ins (login, logout, whoami, tools, skill) is
built at run time from the catalog the API serves, so a tool the backend offers
on the CLI appears here without a new release.
"""

__version__ = "0.1.0"
