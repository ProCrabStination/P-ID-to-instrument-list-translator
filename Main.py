# Main program entry point
# Robert Perry 11/17/2026 starting program creation

# This will handle the pipeline of a job folder to selection of all components.
# this program will need to be installed alongside dependencies, submodules of other projects.

#import all needed modules
import sys
import os
import argparse
import tkinter as tk

#import used submodules
import GUI
import Submodules
import Starter_Extraction
import TurnIn_Extraction
import Starter_Selection
import Pid_Instruments
import Connection_Viewer
import Ocr_Audit


if __name__ == "__main__":
    print("Starting program")

    #step one
    print("Step one, create a UI for finding the job folder.")

    # (P&ID PDF path, turn-in Excel path, options from the Options page), or (None, None, None) if cancelled
    PandID, TurnIn, Options = GUI.run_gui("Welcome")
    if PandID is None or TurnIn is None:
        print("Cancelled by user.")
        sys.exit(0)
    AuditMode = bool((Options or {}).get("audit_mode"))


    print("P&ID PDF:", PandID)
    print("Turn-in workbook:", TurnIn)
    print("Audit Mode:", "on" if AuditMode else "off")

    #step two: process the files for the needed information

    #The P&IDs
    # these can use the same function as the submodule for parsing P&IDs and connections
    # Show in the GUI a loading screen and log, same as the submodule
    # pull all information needed to select each option in Starter_Part_Number_Code.json
    # also pull information that would change anything, like elevation / temperature / area hazardous locations / multiple motors.

    # (the P&ID step now runs below, after the turn-in step)

    # The order Turn In
    # these can use the same function as the submodule for parsing P&IDs and connections
    # Show in the GUI a loading screen and log, same as the submodule
    # pull all information needed to select each option in Starter_Part_Number_Code.json
    # also pull information that would change anything, like elevation / temperature / area hazardous locations / multiple motors.
        
    # The turn-in is an Excel workbook: Excel opens it read-only and hands back every cell listed in
    # TurnIn_Information_to_extract.json (no PDF / AI step, no Excel file created). The filled JSON is saved in Working.


    # print("Step two, read the order turn-in Excel workbook.")
    # TurnInResult = GUI.run_with_progress(
    #     "Reading the order turn-in workbook",
    #     lambda log, stream: TurnIn_Extraction.extract_turnin_info(TurnIn, log=log, stream=stream),
    #     log_name="TurnIn_excel")
    # if TurnInResult is None:
    #     print("Turn-in step failed or was closed early; see the logs folder.")
    #     sys.exit(1)
    # TurnInValues = TurnInResult["values"]  # filled JSON as a Python dict, for the next steps
    # print("Turn-in values JSON:", TurnInResult["values_json"])

    # The P&IDs: instrument tags and their connections to the PLC / panels (vector geometry + OCR, no AI).
    # Saves "<P&ID name> - shapes.json" and "- instrument connections.json / .txt" in Working; the P&ID is only read.
    # The report opens when the step is done, then the viewer to check the links.
    print("Step three, find the P&ID instrument tags and their PLC / panel connections.")
    PidInstruments = None   # the AI instrument list (below) is not run in this version
    # Audit Mode on: the audit window shows every step as it runs: the page OCR, then each symbol's OCR (what was on
    # the page, the image OCR read, what it found, the role it gives) and, at the end, each symbol's PLC / panel links.
    # Audit Mode off: the plain progress window with the log (no audit pictures are made).
    if AuditMode:
        PidLinks = Ocr_Audit.run_with_audit(
            "P&ID instruments and PLC / panel connections",
            lambda log, stream, audit: Pid_Instruments.extract_instrument_connections(PandID, log=log, audit=audit),
            log_name="PandID_instruments")
    else:
        PidLinks = GUI.run_with_progress(
            "P&ID instruments and PLC / panel connections",
            lambda log, stream: Pid_Instruments.extract_instrument_connections(PandID, log=log),
            log_name="PandID_instruments")
    if PidLinks is None:
        print("P&ID step failed or was closed early. See Working\\logs.")
    else:
        print("P&ID instrument connections:", PidLinks["connections"])
        print("P&ID instrument report:", PidLinks["report"])
        os.startfile(str(PidLinks["report"]))  # open the readable report
        for Controller in PidLinks["links"]["controllers"]:
            print(Controller["id"], Controller["label"], "->", [i["tag"] for i in Controller["instruments"]])

        # Audit Mode: draw every recognised symbol and line to check the result: PLC / panels purple, instruments
        # blue; click a symbol to see its links. Continue goes on.
        if AuditMode:
            Connection_Viewer.run_connection_viewer(PidLinks["shapes"], PidLinks["connections"], search="PLC")

    # Previous P&ID step (AI): the submodule's instrument list transcriber, P&ID stage, exactly as its own window
    # runs it, after an options window. Uncomment to use it again instead of (or after) the PyMuPDF export.
    # PidOptions = Starter_Extraction.run_pid_options(PandID)
    # if PidOptions is None:
    #     print("P&ID step skipped.")
    # else:
    #     PidInstruments = GUI.run_with_progress(
    #         "P&ID instrument list transcription",
    #         lambda log, stream: Starter_Extraction.transcribe_pid_instruments(log=log, stream=stream, **PidOptions),
    #         log_name="PandID_instruments")
    #     if PidInstruments is None:
    #         print("P&ID step failed or was closed early; continuing with the turn-in only. See Working\\logs.")
    #     else:
    #         print("P&ID instrument list JSON:", PidInstruments)

    # step four: show every extracted turn-in item and P&ID instrument and the part number picked from
    # Starter_Part_Number_Code.json. Everything is editable; Save writes the selection to the Working folder.


    # print("Step four, select the motor starter part number.")
    # Selection = Starter_Selection.run_starter_selection(TurnInValues, PidInstruments)
    # if Selection is None:
    #     print("Starter selection cancelled.")
    #     sys.exit(0)
    # print("Starter part number:", Selection["part_number"])
    # print("Starter selection JSON:", Selection["saved_to"])

    