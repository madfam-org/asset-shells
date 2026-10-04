# Assembly fixtures — provenance and licence

`assembly-commons/` is a byte-identical copy of part of
[madfam-org/solid-hyperobjects](https://github.com/madfam-org/solid-hyperobjects) at commit
`c278900b048e369ca74c9d03171238cec840752f` (main, 2026-10-04; assemblies A and B landed in
`e887d4f`, #124), the same copy the keystone's golden tests use
(`hyperobjects-spec/tests/fixtures/assembly-golden/commons`):

- `assemblies/voron-2-4-class-350-motion-frame/assembly.json` (assembly A) and
  `assemblies/fpv-5in-freestyle/assembly.json` (assembly B);
- the eight cartridges they use: `tslot-corner`, `tslot-2020`, `endstop-mount`,
  `chain-mount`, `nema-bracket`, `motor-soft-mount`, `pcb-standoff`, `battery-pad`.

They are licensed **CERN-OHL-W-2.0** by their authors (see each `project.json`; licence:
<https://spdx.org/licenses/CERN-OHL-W-2.0.html>), not under this repository's AGPL-3.0-only.
They are copied unmodified and used as test inputs only: the tests build the type and
assembly environments from them with the pinned keystone, exactly as a commons publisher
does, so the digests (A `58caf081…`, B `96a7e42d…`) are the ones the commons CI computes.
Do not edit them here; refresh them from the commons.
