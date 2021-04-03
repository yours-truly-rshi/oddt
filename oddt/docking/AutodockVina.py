import sys
import subprocess
import re
import os
import warnings
from tempfile import mkdtemp
from shutil import rmtree
from distutils.spawn import find_executable
from tempfile import gettempdir

from six import string_types

import oddt
from oddt.utils import (is_openbabel_molecule,
                        is_molecule,
                        check_molecule)
from oddt.spatial import rmsd


class autodock_vina(object):
    def __init__(self,
                 protein=None,
                 auto_ligand=None,
                 size=(20, 20, 20),
                 center=(0, 0, 0),
                 exhaustiveness=8,
                 num_modes=9,
                 energy_range=3,
                 seed=None,
                 prefix_dir=None,
                 n_cpu=1,
                 executable=None,
                 autocleanup=True,
                 skip_bad_mols=True):
        """Autodock Vina docking engine, which extends it's capabilities:
        automatic box (auto-centering on ligand).
        Other software compatible with Vina API can also be used (e.g. QuickVina).

        Parameters
        ----------
        protein: oddt.toolkit.Molecule object (default=None)
            Protein object to be used while generating descriptors.

        auto_ligand: oddt.toolkit.Molecule object or string (default=None)
            Ligand use to center the docking box. Either ODDT molecule or
            a file (opened based on extension and read to ODDT molecule).
            Box is centered on geometric center of molecule.

        size: tuple, shape=[3] (default=(20, 20, 20))
            Dimensions of docking box (in Angstroms)

        center: tuple, shape=[3] (default=(0,0,0))
            The center of docking box in cartesian space.

        exhaustiveness: int (default=8)
            Exhaustiveness parameter of Autodock Vina

        num_modes: int (default=9)
            Number of conformations generated by Autodock Vina. The maximum
            number of docked poses is 9 (due to Autodock Vina limitation).

        energy_range: int (default=3)
            Energy range cutoff for Autodock Vina

        seed: int or None (default=None)
            Random seed for Autodock Vina

        prefix_dir: string or None (default=None)
            Temporary directory for Autodock Vina files.
            By default (None) system temporary directory is used,
            for reference see `tempfile.gettempdir`.

        executable: string or None (default=None)
            Autodock Vina executable location in the system.
            It's really necessary if autodetection fails.

        autocleanup: bool (default=True)
            Should the docking engine clean up after execution?

        skip_bad_mols: bool (default=True)
            Should molecules that crash Autodock Vina be skipped.
        """
        self.dir = prefix_dir or gettempdir()
        self._tmp_dir = None
        # define binding site
        self.size = size
        self.center = center
        # center automaticaly on ligand
        if auto_ligand:
            if isinstance(auto_ligand, string_types):
                extension = auto_ligand.split('.')[-1]
                auto_ligand = next(oddt.toolkit.readfile(extension, auto_ligand))
            self.center = auto_ligand.coords.mean(axis=0).round(3)
        # autodetect Vina executable
        if not executable:
            self.executable = find_executable('vina')
            if not self.executable:
                raise Exception('Could not find Autodock Vina binary.'
                                'You have to install it globally or supply binary'
                                'full directory via `executable` parameter.')
        else:
            self.executable = executable
        # detect version
        self.version = (subprocess.check_output([self.executable, '--version'])
                        .decode('ascii').split(' ')[2])
        self.autocleanup = autocleanup
        self.cleanup_dirs = set()

        # share protein to class
        self.protein = None
        self.protein_file = None
        if protein:
            self.set_protein(protein)
        self.skip_bad_mols = skip_bad_mols
        self.n_cpu = n_cpu
        if self.n_cpu > exhaustiveness:
            warnings.warn('Exhaustiveness is lower than n_cpus, thus CPU will '
                          'not be saturated.')

        # pregenerate common Vina parameters
        self.params = []
        self.params += ['--center_x', str(self.center[0]),
                        '--center_y', str(self.center[1]),
                        '--center_z', str(self.center[2])]
        self.params += ['--size_x', str(self.size[0]),
                        '--size_y', str(self.size[1]),
                        '--size_z', str(self.size[2])]
        self.params += ['--exhaustiveness', str(exhaustiveness)]
        if seed is not None:
            self.params += ['--seed', str(seed)]
        if num_modes > 9 or num_modes < 1:
            raise ValueError('The number of docked poses must be between 1 and 9'
                             ' (due to Autodock Vina limitation).')
        self.params += ['--num_modes', str(num_modes)]
        self.params += ['--energy_range', str(energy_range)]

    @property
    def tmp_dir(self):
        if not self._tmp_dir:
            self._tmp_dir = mkdtemp(dir=self.dir, prefix='autodock_vina_')
            self.cleanup_dirs.add(self._tmp_dir)
        return self._tmp_dir

    @tmp_dir.setter
    def tmp_dir(self, value):
        self._tmp_dir = value

    def set_protein(self, protein):
        """Change protein to dock to.

        Parameters
        ----------
        protein: oddt.toolkit.Molecule object
            Protein object to be used.
        """
        # generate new directory
        self._tmp_dir = None
        if protein:
            if isinstance(protein, string_types):
                extension = protein.split('.')[-1]
                if extension == 'pdbqt':
                    self.protein_file = protein
                    self.protein = next(oddt.toolkit.readfile(extension, protein))
                    self.protein.protein = True
                else:
                    self.protein = next(oddt.toolkit.readfile(extension, protein))
                    self.protein.protein = True
            else:
                self.protein = protein

            # skip writing if we have PDBQT protein
            if self.protein_file is None:
                self.protein_file = write_vina_pdbqt(self.protein, self.tmp_dir,
                                                     flexible=False)

    def score(self, ligands, protein=None):
        """Automated scoring procedure.

        Parameters
        ----------
        ligands: iterable of oddt.toolkit.Molecule objects
            Ligands to score

        protein: oddt.toolkit.Molecule object or None
            Protein object to be used. If None, then the default
            one is used, else the protein is new default.

        Returns
        -------
        ligands : array of oddt.toolkit.Molecule objects
            Array of ligands (scores are stored in mol.data method)
        """
        if protein:
            self.set_protein(protein)
        if not self.protein_file:
            raise IOError("No receptor.")
        if is_molecule(ligands):
            ligands = [ligands]
        ligand_dir = mkdtemp(dir=self.tmp_dir, prefix='ligands_')
        output_array = []
        for n, ligand in enumerate(ligands):
            check_molecule(ligand, force_coords=True)
            ligand_file = write_vina_pdbqt(ligand, ligand_dir, name_id=n)
            try:
                scores = parse_vina_scoring_output(
                    subprocess.check_output([self.executable, '--score_only',
                                             '--receptor', self.protein_file,
                                             '--ligand', ligand_file] + self.params,
                                            stderr=subprocess.STDOUT))
            except subprocess.CalledProcessError as e:
                sys.stderr.write(e.output.decode('ascii'))
                if self.skip_bad_mols:
                    continue
                else:
                    raise Exception('Autodock Vina failed. Command: "%s"' %
                                    ' '.join(e.cmd))
            ligand.data.update(scores)
            output_array.append(ligand)
        rmtree(ligand_dir)
        return output_array

    def dock(self, ligands, protein=None):
        """Automated docking procedure.

        Parameters
        ----------
        ligands: iterable of oddt.toolkit.Molecule objects
            Ligands to dock

        protein: oddt.toolkit.Molecule object or None
            Protein object to be used. If None, then the default one
            is used, else the protein is new default.

        Returns
        -------
        ligands : array of oddt.toolkit.Molecule objects
            Array of ligands (scores are stored in mol.data method)
        """
        if protein:
            self.set_protein(protein)
        if not self.protein_file:
            raise IOError("No receptor.")
        if is_molecule(ligands):
            ligands = [ligands]
        ligand_dir = mkdtemp(dir=self.tmp_dir, prefix='ligands_')
        output_array = []
        for n, ligand in enumerate(ligands):
            check_molecule(ligand, force_coords=True)
            ligand_file = write_vina_pdbqt(ligand, ligand_dir, name_id=n)
            ligand_outfile = ligand_file[:-6] + '_out.pdbqt'
            try:
                scores = parse_vina_docking_output(
                    subprocess.check_output([self.executable, '--receptor',
                                             self.protein_file,
                                             '--ligand', ligand_file,
                                             '--out', ligand_outfile] +
                                            self.params +
                                            ['--cpu', str(self.n_cpu)],
                                            stderr=subprocess.STDOUT))
            except subprocess.CalledProcessError as e:
                sys.stderr.write(e.output.decode('ascii'))
                if self.skip_bad_mols:
                    continue  # TODO: print some warning message
                else:
                    raise Exception('Autodock Vina failed. Command: "%s"' %
                                    ' '.join(e.cmd))

            # docked conformations may have wrong connectivity - use source ligand
            if is_openbabel_molecule(ligand):
                # find the order of PDBQT atoms assigned by OpenBabel
                with open(ligand_file) as f:
                    write_order = [int(line[7:12].strip())
                                   for line in f
                                   if line[:4] == 'ATOM']
                new_order = sorted(range(len(write_order)),
                                   key=write_order.__getitem__)
                new_order = [i + 1 for i in new_order]  # OBMol has 1 based idx

                assert len(new_order) == len(ligand.atoms)

            docked_ligands = oddt.toolkit.readfile('pdbqt', ligand_outfile)
            for docked_ligand, score in zip(docked_ligands, scores):
                # Renumber atoms to match the input ligand
                if is_openbabel_molecule(docked_ligand):
                    docked_ligand.OBMol.RenumberAtoms(new_order)
                # HACK: copy docked coordinates onto source ligand
                # We assume that the order of atoms match between ligands
                clone = ligand.clone
                clone.clone_coords(docked_ligand)
                clone.data.update(score)

                # Calculate RMSD to the input pose
                try:
                    clone.data['vina_rmsd_input'] = rmsd(ligand, clone)
                    clone.data['vina_rmsd_input_min'] = rmsd(ligand, clone,
                                                             method='min_symmetry')
                except Exception:
                    pass
                output_array.append(clone)
        rmtree(ligand_dir)
        return output_array

    def clean(self):
        for d in self.cleanup_dirs:
            rmtree(d)

    def predict_ligand(self, ligand):
        """Local method to score one ligand and update it's scores.

        Parameters
        ----------
        ligand: oddt.toolkit.Molecule object
            Ligand to be scored

        Returns
        -------
        ligand: oddt.toolkit.Molecule object
            Scored ligand with updated scores
        """
        return self.score([ligand])[0]

    def predict_ligands(self, ligands):
        """Method to score ligands lazily

        Parameters
        ----------
        ligands: iterable of oddt.toolkit.Molecule objects
            Ligands to be scored

        Returns
        -------
        ligand: iterator of oddt.toolkit.Molecule objects
            Scored ligands with updated scores
        """
        return self.score(ligands)


def write_vina_pdbqt(mol, directory, flexible=True, name_id=None):
    """Write single PDBQT molecule to a given directory. For proteins use
    `flexible=False` to avoid encoding torsions. Additionally an name ID can
    be appended to a name to avoid conflicts.
    """
    if name_id is None:
        name_id = ''

    # We expect name such as 0_ZINC123456.pdbqt or simply ZINC123456.pdbqt if no
    # name_id is specified. All non alpha-numeric signs are replaced with underscore.
    mol_file = ('_'.join(filter(None, [str(name_id),
                                       re.sub('[^A-Za-z0-9]+', '_', mol.title)]
                                )) + '.pdbqt')
    # prepend path to filename
    mol_file = os.path.join(directory, mol_file)

    if is_openbabel_molecule(mol):
        if flexible:
            # auto bonding (b), perserve atom indices (p) and Hs (h)
            kwargs = {'opt': {'b': None, 'p': None, 'h': None}}
        else:
            # for proteins write rigid mol (r) and combine all frags in one (c)
            kwargs = {'opt': {'r': None, 'c': None, 'h': None}}

    else:
        kwargs = {'flexible': flexible}

    mol.write('pdbqt', mol_file, overwrite=True, **kwargs)
    return mol_file


def parse_vina_scoring_output(output):
    """Function parsing Autodock Vina scoring output to a dictionary

    Parameters
    ----------
    output : string
        Autodock Vina standard ouptud (STDOUT).

    Returns
    -------
    out : dict
        dicitionary containing scores computed by Autodock Vina
    """
    out = {}
    r = re.compile(r'^(Affinity:|\s{4})')
    for line in output.decode('ascii').split('\n')[13:]:  # skip some output
        if r.match(line):
            m = line.replace(' ', '').split(':')
            if m[0] == 'Affinity':
                m[1] = m[1].replace('(kcal/mol)', '')
            out[str('vina_' + m[0].lower())] = float(m[1])
    return out


def parse_vina_docking_output(output):
    """Function parsing Autodock Vina docking output to a dictionary

    Parameters
    ----------
    output : string
        Autodock Vina standard ouptud (STDOUT).

    Returns
    -------
    out : dict
        dicitionary containing scores computed by Autodock Vina
    """
    out = []
    r = re.compile(r'^\s+\d\s+')
    for line in output.decode('ascii').split('\n')[13:]:  # skip some output
        if r.match(line):
            s = line.split()
            out.append({'vina_affinity': s[1],
                        'vina_rmsd_lb': s[2],
                        'vina_rmsd_ub': s[3]})
    return out
