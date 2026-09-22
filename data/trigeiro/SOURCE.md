# Trigeiro F1

Single-machine capacitated lot-sizing with setup times.

Source: William W. Trigeiro, L. Joseph Thomas, and John O. McClain, “Capacitated Lot Sizing with Setup Times,” *Management Science* 35(3):353–366, 1989.

This copy is the `F1.DAT` file from the public mirror <https://github.com/gsamaro/trigeiro_fdata> (`data/F1.DAT`, commit history on that repository). Numeric rows only; the mirror’s column-name footer is not included. The file is their redistribution of the published test set, not data collected for this repository.

Format, one record per line: item count and period count; a skipped marker; one capacity used in every period; then one row per item (unit production time, holding cost, setup time, setup cost); then one row per period with one demand column per item.
