# asset-shells

MADFAM's asset-shell service: it stores and serves Asset Administration Shell (AAS) documents for the
designs in the MADFAM commons (type shells) and for the parts made from them (instance shells and
product passports).

> **Boundary note (public-safe).** This is a public repository. Operational detail — cluster topology,
> secret paths, client identifiers, operator runbooks — lives in MADFAM's private operations repository
> and is only pointed to from here. Never copy that detail into this repository.

Free software under the [GNU AGPL-3.0-only](./LICENSE). The service is in its first build; see the
`feat/service-v1` branch for the code.
