# Assembly fixtures — provenance and licence

`assembly-commons/` is a byte-identical copy of part of
[madfam-org/solid-hyperobjects](https://github.com/madfam-org/solid-hyperobjects) at commit
`00e17651b95ef9889a67a22e1024a4a5923f666d` (main, 2026-10-04; assemblies A and B landed in
`e887d4f`, #124; B carries the camera cage since `ea0a835`, #136), the same copy the keystone's golden tests use
(`hyperobjects-spec/tests/fixtures/assembly-golden/commons`):

- `assemblies/voron-2-4-class-350-motion-frame/assembly.json` (assembly A) and
  `assemblies/fpv-5in-freestyle/assembly.json` (assembly B);
- the nine cartridges they use: `tslot-corner`, `tslot-2020`, `endstop-mount`,
  `chain-mount`, `nema-bracket`, `motor-soft-mount`, `pcb-standoff`, `battery-pad`,
  `fpv-camera-cage`.

They are licensed **CERN-OHL-W-2.0** by their authors (see each `project.json`; licence:
<https://spdx.org/licenses/CERN-OHL-W-2.0.html>), not under this repository's AGPL-3.0-only.
They are copied unmodified and used as test inputs only: the tests build the type and
assembly environments from them with the pinned keystone, exactly as a commons publisher
does, so the digests are the commons CI's for the same keystone (A `24322cc0…` on the pinned keystone's
catalog, `58caf081…` before hyperobjects-spec#44 changed the `extrusion-2020` entry; B `96166430…` on the
pinned keystone's catalog, `f0db7bdb…` at the commons' SPEC_PIN 8c12194, before hyperobjects-spec#41).
Do not edit them here; refresh them from the commons.
