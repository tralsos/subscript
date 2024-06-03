import argparse
import logging
import os
from datetime import date
import sys
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
import yaml
from pydantic import BaseModel, Field, FilePath, model_validator, field_validator
from typing_extensions import Annotated

from resdata.resfile import ResdataFile
from resdata.grid import Grid
from resdata.gravimetry import ResdataGrav, ResdataSubsidence

import xtgeo

import subscript
logger = subscript.getLogger(__name__)

# Constant for subsidence modelling, not influencing results
# since subsidence is calculated from porevolume change
# therefore defaulted
DUMMY_YOUNGS = 0.5

PREFIX_GRAVSURF = "all--delta_gravity_"
PREFIX_SUBSSURF = "all--subsidence"

DESCRIPTION = """
Modelling maps of gravity change and subsidence from flow
simulation output (EGRID, INIT and UNRST files).

The script reads flow simulation results and a yaml configuration file specifying input
and calculation parameters. Output is surfaces in irap binary format.
"""

EPILOGUE = """
.. code-block:: yaml

  # Example config file for grav_subs_maps

  input:
      diffdates:
        - [2020-07-01, 2018-01-01] # Difference date to model. Must exist in UNRST file.
      seabed_map: seabed.gri  # Path to file with seabed, irap binary format.
                              # Also used as map template

    calculations:
      poisson_ratio: 0.45  # For subsidence calulcations, used in Geertsma model
      coarsening: 8        # Coarsening factor for maps to speed up calculations
      phases: ['gas', 'oil','water', 'total']  # One map for each phase specified

"""

CATEGORY = "modelling.reservoir"

EXAMPLES = """
.. code-block:: console

 FORWARD_MODEL GRAV_SUBS_MAPS(<UNRST_FILE>=<ECLBASE>.UNRST, <GRAV_CONFIG>=grav_subs_maps.yml, <OUTPUTDIR>=share/results/maps)


where ``ECLBASE`` is already defined in your ERT config, pointing to the flowsimulator
basename relative to ``RUNPATH``, grav_subs_maps.yml is a YAML file defining
the inputs and modelling parameters and ``OUTPUTDIR`` is the path to the output folder.

The directory to export maps to must exist.
"""  # noqa


class GravInput(BaseModel):
    diffdates: List[List[date]]
    seabed_map: FilePath

class GravCalc(BaseModel):
    poisson_ratio: Annotated[float, Field(strict=True, ge=0, le=0.5)]
    coarsening: Optional[Annotated[int, Field(strict=True, ge=1)]] = None
    phases: List[str]

    @field_validator("phases")
    @classmethod
    def check_phases(cls, phases: List[str]) -> List[str]:
        allowed_phases = ['oil', 'gas', 'water','total']
        for item in phases:
            assert (
                item in allowed_phases
            ), f"allowed phases are {str(allowed_phases)}"
        return phases


class GravMapsConfig(BaseModel):
    input: GravInput
    calculations: GravCalc



def get_parser() -> argparse.ArgumentParser:
    """Function to create the argument parser that is going to be served to the user.

    Returns:
        argparse.ArgumentParser: The argument parser to be served

    """
    parser = argparse.ArgumentParser(
        prog="grav_subs_maps.py",
        description=DESCRIPTION,
        epilog=EPILOGUE,
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "UNRSTfile",
        type=str,
        help="Path to flowsimulator UNRST file"
    )
    parser.add_argument(
        "-c",
        "-C",
        "--configfile",
        type=str,
        help="Name of YAML config file",
        required=True,
    )
    parser.add_argument(
        "-o",
        "--outputdir",
        type=str,
        help="Path to directory for output maps. Directory must exist.",
        default="./",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s (subscript version " + subscript.__version__ + ")",
    )
    return parser


def main() -> None:
    """Invocated from the command line, parsing command line arguments"""
    parser = get_parser()
    args = parser.parse_args()

    logger.setLevel(logging.INFO)

    # parse the config file
    if not Path(args.configfile).exists():
        sys.exit("No such file:" + args.configfile)
    config = yaml.safe_load(Path(args.configfile).read_text(encoding="utf8"))
    cfg = GravMapsConfig.model_validate(config).model_dump()

    if not Path(args.outputdir).exists():
        sys.exit("Output folder does not exist:" + args.outputdir)
    if not Path(args.UNRSTfile).exists():
        sys.exit("UNRST file does not exist:" + args.UNRSTfile)

    main_gravmaps(args.UNRSTfile, cfg, Path(args.outputdir))


def main_gravmaps(unrst_file: str, cfg: Dict[str, Any], output_folder: Path) -> None:
    """
    Process a configuration, model gravity and subsidence surfaces and write to disk.

    Args:
        resdata: Path to flow simulation UNRST file
        cfg: Configuration for modelling
    """

    # Read inputs and calculation parameters
    input_diffdates = cfg['input']['diffdates']
    map_template = cfg['input']['seabed_map']
    coarsening = cfg['calculations']['coarsening']
    phases = cfg['calculations']['phases']
    poisson_ratio = cfg['calculations']['poisson_ratio']

    # Read seabed map and coarsen
    seabed = xtgeo.surface_from_file(map_template)
    seabed.coarsen(coarsening)


    if isinstance(unrst_file,str):
        restart_file = unrst_file[:-6] + ".UNRST"
        egrid_file = unrst_file[:-6] + ".EGRID"
        init_file = unrst_file[:-6] + ".INIT"
        grid = Grid(egrid_file)
        init = ResdataFile(init_file)
        rest = ResdataFile(restart_file)

    restart_index = {}

    # From restart datetime format to YYYYMMDD as key
    for i, restart_date in enumerate(rest.dates):
        restart_index[restart_date.strftime("%Y%m%d")] = i

    diffdates=[]
    # Convert dates from datetime format to strings
    logger.info("Will do modelling for diffdates: ")
    for diffdate in input_diffdates:
        diff = [diffdate[0].strftime("%Y%m%d"),diffdate[1].strftime("%Y%m%d")]
        diffdates.append(diff)
        logger.info(
            f'{diffdate[0]}_{diffdate[1]}'
            )

    grav = ResdataGrav(grid,init)
    subsidence = ResdataSubsidence(grid, init)

    added_dates=[]

    for diffdate in diffdates:
        for date in diffdate: # base and monitor
            rsb = rest.restartView(0)
            if date not in added_dates:
                if date in restart_index.keys():
                    rsb = rest.restartView( restart_index[date])
                    grav.add_survey_RFIP(date, rsb)
                    subsidence.add_survey_PRESSURE(date, rsb)
                    added_dates.append(date)
                else:
                    logger.error(
                        f"Date {date} specified but not"
                        "found in UNRST file."
                    )
                    sys.exit(1)
    phase_code = {'oil':1, 'gas':2, 'water':4, 'total':7}

    # Gravity
    for diffdate in diffdates:
        for phase in phases:
            logger.info(f'Calculating delta gravity map from {phase} for {diffdate[0]}_{diffdate[1]}')
            dgsim = seabed.copy()
            df_dgsim = dgsim.get_dataframe()
            dgsim_series = []
            for index,row in df_dgsim.iterrows():
                dgsim_series.append(grav.eval(diffdate[1], diffdate[0],
                                              (row['X_UTME'], row['Y_UTMN'], row['VALUES']),
                                              phase_mask=phase_code[phase]))
            dgsim.values = dgsim_series
            filename = PREFIX_GRAVSURF + phase+"--" + diffdate[0] + "_" + diffdate[1]+".gri"
            dgsim.to_file(os.path.join(output_folder, filename))

    # Subsidence
    for diffdate in diffdates:
        logger.info(f'Calculating subsidence map for {diffdate[0]}_{diffdate[1]}')
        dzsim = seabed.copy()
        df_dzsim = dzsim.get_dataframe()
        dzsim_series = []
        for index,row in df_dzsim.iterrows():
            dzsim_series.append(
                subsidence.eval_geertsma_rporv(
                    diffdate[1], diffdate[0],
                    (row['X_UTME'], row['Y_UTMN'], row['VALUES']),
                    DUMMY_YOUNGS, poisson_ratio, row['VALUES']
                    ))

        dzsim.values = [i*100 for i in dzsim_series]  # From m to cms

        filename = PREFIX_SUBSSURF +"--" + diffdate[0] + "_" + diffdate[1]+".gri"
        dzsim.to_file(os.path.join(output_folder, filename))



    logger.info(
        "Done; All gravity and subsidence maps written to folder: %s",
        str(output_folder),
    )


if __name__ == "__main__":
    main()
