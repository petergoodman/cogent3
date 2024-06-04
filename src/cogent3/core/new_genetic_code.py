"""Translates RNA or DNA string to amino acid sequence.

Notes
 ----

* is used to denote termination (as per NCBI standard).

Although the genetic code objects convert DNA to RNA and vice
versa, lists of codons that they produce will be provided in DNA format.
"""

import collections
import contextlib
import dataclasses
import itertools
import typing

import numpy

from cogent3.core import new_alphabet, new_moltype
from cogent3.util.table import Table


OptStr = typing.Optional[str]
SetStr = typing.Set[str]
ConverterType = typing.Callable[[bytes, bytes], bytes]
StrORInt = typing.Union[str, int]

DNA = new_moltype.get_moltype("dna")
RNA = new_moltype.get_moltype("rna")


class GeneticCodeError(Exception):
    pass


class GeneticCodeInitError(ValueError, GeneticCodeError):
    pass


class InvalidCodonError(KeyError, GeneticCodeError):
    pass


def _make_mappings(
    codons: new_alphabet.KmerAlphabet, code_sequence: str
) -> typing.Tuple[typing.Dict[str, str], typing.Dict[str, SetStr], SetStr]:
    """makes amino acid / codon mappings and stop codon group

    Parameters
    ----------
    codons
        alphabet of codons
    code_sequence
        64-character string containing NCBI representation of the genetic code.

    Returns
    -------
    codon to amino acid mapping, the reverse mapping, the set of stop codons
    """
    stops = set()
    codon_to_aa = {}
    aa_to_codon = collections.defaultdict(set)
    for codon, aa in zip(codons, code_sequence):
        if aa == "*":
            stops.add(codon)
        codon_to_aa[codon] = aa
        aa_to_codon[aa].add(codon)
    return codon_to_aa, aa_to_codon, stops


def _get_start_codon_indices(start_codon_map: str) -> tuple[int, ...]:
    return tuple(i for i, start in enumerate(start_codon_map) if start == "M")


def _make_converter(
    kmer_alpha: new_alphabet.KmerAlphabet, codons: tuple[str, ...], code_sequence: str
) -> typing.Callable[[bytes, bytes], bytes]:
    assert len(codons) == len(code_sequence) == 64
    # we get the index of the codon in the kmer alphabet
    kmers = numpy.array(
        [kmer_alpha.to_index(codon) for codon in codons], dtype=numpy.uint8
    )
    return new_alphabet.convert_alphabet(kmers.tobytes(), code_sequence.encode("utf8"))


@dataclasses.dataclass
class GeneticCode:
    """Holds codon to amino acid mapping, and vice versa."""

    ID: int
    name: str
    ncbi_code_sequence: dataclasses.InitVar[str]
    ncbi_start_codon_map: dataclasses.InitVar[str]
    moltype: new_moltype.MolType = new_moltype.DNA
    _codon_to_aa: typing.Dict[str, str] = dataclasses.field(init=False, default=None)
    _aa_to_codon: typing.Dict[str, typing.List[str]] = dataclasses.field(
        init=False, default=None
    )
    _sense_codons: SetStr = dataclasses.field(init=False, default=None)
    _stop_codons: SetStr = dataclasses.field(init=False, default=None)
    _start_codons: SetStr = dataclasses.field(init=False, default=None)
    codons: new_alphabet.KmerAlphabet = dataclasses.field(init=False, default=None)
    anticodons: typing.Tuple[str, ...] = dataclasses.field(init=False, default=None)
    # callables for translating on the plus strand, or the minus strand
    _translate_plus: ConverterType = dataclasses.field(init=False, default=None)
    _translate_minus: ConverterType = dataclasses.field(init=False, default=None)

    def __post_init__(self, ncbi_code_sequence: str, ncbi_start_codon_map: str):
        trinuc_alpha = self.moltype.alphabet.get_kmer_alphabet(k=3, include_gap=False)
        self._codon_to_aa, self._aa_to_codon, self._stop_codons = _make_mappings(
            trinuc_alpha, ncbi_code_sequence
        )
        self.codons = trinuc_alpha
        self._start_codons = {
            self.codons[i] for i in _get_start_codon_indices(ncbi_start_codon_map)
        }
        self._sense_codons = tuple(
            c for c in self.codons if self._codon_to_aa[c] != "*"
        )
        self.anticodons = tuple(self.moltype.rc(codon) for codon in self.codons)
        self._translate_plus = _make_converter(
            trinuc_alpha, self.codons, ncbi_code_sequence
        )
        self._translate_minus = _make_converter(
            trinuc_alpha, self.anticodons, ncbi_code_sequence
        )

    @property
    def stop_codons(self) -> SetStr:
        return self._stop_codons

    @property
    def start_codons(self) -> SetStr:
        return self._start_codons

    @property
    def sense_codons(self) -> SetStr:
        return self._sense_codons

    def to_table(self):
        """returns aa to codon mapping as a cogent3 Table"""
        from cogent3.core.moltype import IUPAC_PROTEIN_code_aa

        rows = []
        headers = ["aa", "IUPAC code", "codons"]
        for code, aa in IUPAC_PROTEIN_code_aa.items():
            codons = ",".join(self[code])
            row = [aa, code, codons]
            rows.append(row)
        return Table(header=headers, data=rows, title=self.name)

    def __repr__(self):
        display = self.to_table()
        return str(display)

    def _repr_html_(self):
        """Returns the html representation of GeneticCode."""
        display = self.to_table()
        display.set_repr_policy(show_shape=False)
        return display._repr_html_()

    def __eq__(self, other):
        """Allows two GeneticCode objects to be compared to each other.
        Two GeneticCode objects are equal if they have equal CodeSequences.
        """
        return str(self) == str(other)

    def __getitem__(self, item):
        """Returns amino acid corresponding to codon, or codons for an aa.

        Returns [] for empty list of codons, 'X' for unknown amino acid.
        """
        item = str(item)
        if len(item) == 1:  # amino acid
            return self._aa_to_codon.get(item, set())

        if len(item) != 3:
            raise InvalidCodonError(f"Codon or aa {item} has wrong length")

        key = item.upper()
        key = key.replace("U", "T")
        return self._codon_to_aa.get(key, "X")

    def translate(self, dna, start: int = 0, rc: bool = False) -> str:
        """Translates DNA to protein with current GeneticCode.

        Parameters
        ----------
        dna
            a string of nucleotides
        start
            position to begin translation (used to implement frames)
        rc
            if True, returns the translation of the reverse complement sequence

        Notes
        -----
        Sequences are truncated to be a multiple of 3.

        Returns
        -------
        String containing amino acid sequence.
        """
        if start:
            dna = dna[start:]

        diff = len(dna) % 3
        if diff != 0:
            dna = dna[:-diff]

        # convert to indices and then bytes
        seq = self.codons.to_indices(dna).tobytes()

        if rc:
            return self._translate_minus(seq).decode("utf8")[::-1]

        return self._translate_plus(seq).decode("utf8")

    def sixframes(self, seq: str) -> typing.Iterable[typing.Tuple[str, int, str]]:
        """Returns the six reading frames of the genetic code.

        Returns
        -------
        A dictionary with keys (strand, start) where strand is "+"/"-"
        """

        for strand, start in itertools.product(("+", "-"), range(3)):
            yield strand, start, self.translate(seq, start, rc=strand == "-")


_mapping_cols = "ncbi_code_sequence", "ID", "name", "ncbi_start_codon_map"
# code mappings are based on the product of bases in order TCAG
code_mapping = (
    (
        "FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        1,
        "Standard",
        "---M---------------M---------------M----------------------------",
    ),
    (
        "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIMMTTTTNNKKSS**VVVVAAAADDEEGGGG",
        2,
        "Vertebrate Mitochondrial",
        "--------------------------------MMMM---------------M------------",
    ),
    (
        "FFLLSSSSYY**CCWWTTTTPPPPHHQQRRRRIIMMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        3,
        "Yeast Mitochondrial",
        "----------------------------------MM---------------M------------",
    ),
    (
        "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        4,
        "Mold Mitochondrial; Protozoan Mitochondrial; Coelenterate "
        "Mitochondrial; Mycoplasma; Spiroplasma",
        "--MM---------------M------------MMMM---------------M------------",
    ),
    (
        "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIMMTTTTNNKKSSSSVVVVAAAADDEEGGGG",
        5,
        "Invertebrate Mitochondrial",
        "---M----------------------------MMMM---------------M------------",
    ),
    (
        "FFLLSSSSYYQQCC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        6,
        "Ciliate Nuclear; Dasycladacean Nuclear; Hexamita Nuclear",
        "-----------------------------------M----------------------------",
    ),
    (
        "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIIMTTTTNNNKSSSSVVVVAAAADDEEGGGG",
        9,
        "Echinoderm Mitochondrial; Flatworm Mitochondrial",
        "-----------------------------------M---------------M------------",
    ),
    (
        "FFLLSSSSYY**CCCWLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        10,
        "Euplotid Nuclear",
        "-----------------------------------M----------------------------",
    ),
    (
        "FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        11,
        "Bacterial, Archaeal and Plant Plastid",
        "---M---------------M------------MMMM---------------M------------",
    ),
    (
        "FFLLSSSSYY**CC*WLLLSPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        12,
        "Alternative Yeast Nuclear",
        "-------------------M---------------M----------------------------",
    ),
    (
        "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIMMTTTTNNKKSSGGVVVVAAAADDEEGGGG",
        13,
        "Ascidian Mitochondrial",
        "---M------------------------------MM---------------M------------",
    ),
    (
        "FFLLSSSSYYY*CCWWLLLLPPPPHHQQRRRRIIIMTTTTNNNKSSSSVVVVAAAADDEEGGGG",
        14,
        "Alternative Flatworm Mitochondrial",
        "-----------------------------------M----------------------------",
    ),
    (
        "FFLLSSSSYY*QCC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        15,
        "Blepharisma Macronuclear",
        "-----------------------------------M----------------------------",
    ),
    (
        "FFLLSSSSYY*LCC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        16,
        "Chlorophycean Mitochondrial",
        "-----------------------------------M----------------------------",
    ),
    (
        "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIMMTTTTNNNKSSSSVVVVAAAADDEEGGGG",
        21,
        "Trematode Mitochondrial",
        "-----------------------------------M---------------M------------",
    ),
    (
        "FFLLSS*SYY*LCC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        22,
        "Scenedesmus obliquus Mitochondrial",
        "-----------------------------------M----------------------------",
    ),
    (
        "FF*LSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        23,
        "Thraustochytrium Mitochondrial",
        "--------------------------------M--M---------------M------------",
    ),
    (
        "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSSKVVVVAAAADDEEGGGG",
        24,
        "Rhabdopleuridae Mitochondrial",
        "---M---------------M---------------M---------------M------------",
    ),
    (
        "FFLLSSSSYY**CCGWLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        25,
        "Candidate Division SR1 and Gracilibacteria",
        "---M-------------------------------M---------------M------------",
    ),
    (
        "FFLLSSSSYY**CC*WLLLAPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        26,
        "Pachysolen tannophilus Nuclear",
        "-------------------M---------------M----------------------------",
    ),
    (
        "FFLLSSSSYYQQCCWWLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        27,
        "Karyorelict Nuclear",
        "-----------------------------------M----------------------------",
    ),
    (
        "FFLLSSSSYYQQCCWWLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        28,
        "Condylostoma Nuclear",
        "-----------------------------------M----------------------------",
    ),
    (
        "FFLLSSSSYYYYCC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        29,
        "Mesodinium Nuclear",
        "-----------------------------------M----------------------------",
    ),
    (
        "FFLLSSSSYYEECC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        30,
        "Peritrich Nuclear",
        "-----------------------------------M----------------------------",
    ),
    (
        "FFLLSSSSYYEECCWWLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        31,
        "Blastocrithidia Nuclear",
        "-----------------------------------M----------------------------",
    ),
    (
        "FFLLSSSSYY*WCC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        32,
        "Balanophoraceae Plastid",
        "---M---------------M------------MMMM---------------M------------",
    ),
    (
        "FFLLSSSSYYY*CCWWLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSSKVVVVAAAADDEEGGGG",
        33,
        "Cephalodiscidae Mitochondrial",
        "---M---------------M---------------M---------------M------------",
    ),
)
_CODES = {}
for mapping in code_mapping:
    code = GeneticCode(**dict(zip(_mapping_cols, mapping)))
    _CODES[code.ID] = code
    _CODES[code.name] = code


DEFAULT = _CODES[1]


def get_code(code_id: StrORInt = 1) -> GeneticCode:
    """returns the genetic code

    Parameters
    ----------
    code_id
        genetic code identifier, name, number or string(number), defaults to
        standard genetic code
    """
    with contextlib.suppress(ValueError):
        code_id = int(code_id)

    if code_id not in _CODES:
        raise GeneticCodeError(f"Unknown genetic code {code_id}")
    return _CODES[code_id]


def available_codes():
    """returns Table listing the available genetic codes"""
    from cogent3.util.table import Table

    rows = [(k, code.name) for k, code in _CODES.items() if isinstance(k, int)]
    header = ["Code ID", "Name"]
    return Table(
        header=header,
        data=rows,
        index_name="Code ID",
        title="Specify a genetic code using either 'Name' or "
        "Code ID (as an integer or string)",
    )
