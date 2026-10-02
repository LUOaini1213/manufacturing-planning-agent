# Trigeiro F1

Single-machine capacitated lot-sizing with setup times.

Source: William W. Trigeiro, L. Joseph Thomas, and John O. McClain, “Capacitated Lot Sizing with Setup Times,” *Management Science* 35(3):353–366, 1989.

This copy is the `F1.DAT` file from the public mirror, pinned to [commit a54b0fb51657fb93530b60faa794893e3d0bfae8](https://github.com/gsamaro/trigeiro_fdata/blob/a54b0fb51657fb93530b60faa794893e3d0bfae8/data/F1.DAT). Numeric rows only; the mirror’s column-name footer is not included. The file is their redistribution of the published test set, not data collected for this repository.

Rechecked on 2026-10-03: all 24 numeric rows and 118 numeric tokens match the pinned source in order. The source file has SHA256 `518ce9b105923f960e7c5e650ae913b55ee4f94692eccfeb4507da063bf8c9e9`; whitespace and the footer differ from this repository's numeric-only copy.

Format, one record per line: item count and period count; a skipped marker; one capacity used in every period; then one row per item (unit production time, holding cost, setup time, setup cost); then one row per period with one demand column per item.
