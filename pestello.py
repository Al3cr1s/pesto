#!/usr/bin/env python
# -*- coding: utf-8 -*-

import csv
import argparse
import os

RED = "\033[31;40m"
END_ESCAPE = "\033[0;0m"


def get_files(paths):
    results = []
    serials = set()
    errors = False

    try:
        for file in paths:
            file: str
            if os.path.isdir(file):
                for filename in os.listdir(file):
                    filename = file.rstrip('/') + '/' + filename
                    parse_file(filename, results, serials)
            elif os.path.isfile(file):
                parse_file(file, results, serials)
            else:
                print(f"{file} is not a file nor a directory")
                errors = True
    except KeyboardInterrupt:
        pass
    except EOFError:
        pass

    print(f"{len(serials)} unique disks parsed, {len(results)} labeled")

    header = []
    header_set = set()
    for result in results:
        for k in result:
            if k != 'Status' and k not in header_set:
                header_set.add(k)
                header.append(k)
    header_set.add('Status')
    header.append('Status')
    print(header)

    with open('labeled.csv', 'w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL, fieldnames=header)
        writer.writeheader()

        for result in results:
            writer.writerow(result)

    if errors:
        exit(1)


def parse_file(filename: str, results: list, serials: set):
    attributes = {
        "Start_Stop_Count",
        "Reallocated_Sector_Ct",
        "Seek_Error_Rate",
        "Power_On_Hours",
        "Power_Cycle_Count",
        "SSD_Life_Left",
        "Lifetime_Writes_GiB",
        "Power_On_Hours",
        "Load_Cycle_Count",
        "Reallocated_Event_Count",
        "Current_Pending_Sector",
        "Offline_Uncorrectable"
    }

    found = dict()
    errors = 0

    with open(filename, 'r') as f:
        info_section = False
        data_section = False
        errors_section = False
        for line in f:
            line: str
            if "=== START OF INFORMATION SECTION ===" in line:
                info_section = True
                data_section = False
                errors_section = False
                continue
            if "=== START OF READ SMART DATA SECTION ===" in line:
                info_section = False
                data_section = True
                errors_section = False
                continue
            if "SMART Error Log Version" in line:
                info_section = False
                data_section = False
                errors_section = True
                continue
            if info_section:
                if 'Model Family:  ' in line:
                    val = line.split('  ', 1)[1]
                    found["Model_Family"] = val.strip()

                if 'Serial Number' in line:
                    val = line.split('  ', 1)[1]
                    found["Serial_Number"] = val.strip()
                continue
            if data_section:
                for attr in attributes:
                    if attr in line:
                        val = line.split(" ")[-1].strip()
                        if attr == "Power_On_Hours":
                            found["Power_On_Hours_Exact"] = "false"
                            if 'h' in val:
                                val = val.split("h")[0]
                                found["Power_On_Hours_Exact"] = "true"
                        found[attr] = val.rstrip()
                continue
            if errors_section:
                if 'Error: UNC' in line:
                    errors += 1

    found["Errors_UNC"] = str(errors)

    if "Serial_Number" in found:
        if found["Serial_Number"] in serials:
            print(f"Skipping {found['Serial_Number']} in {filename}")
            return
        else:
            serials.add(found["Serial_Number"])

    for k in found:
        if k == "Power_On_Hours":
            details = f" ({int(found[k])/24:.2f} server days, {int(found[k])/8/304:.2f} office years)"
            if found["Power_On_Hours_Exact"] == "false":
                if 20 > int(found[k]) / 60 / 24 / 365 > 1:
                    details += f" (or, if minutes, {int(found[k]) / 60 / 24:.2f} server days, {int(found[k]) / 60 / 8 / 304:.2f} office years)"
        else:
            details = ""
        if found[k].isnumeric() and int(found[k]) != 0:
            color1 = RED
            color2 = END_ESCAPE
        else:
            color1 = color2 = ""
        print(f"{k}: {color1}{found[k]}{color2}{details}")
    print(f"File is {filename}")

    answered = False
    while not answered:
        r = input("Is it OK, OLD, FAIL or discard? [K,O,F,X] ")
        r = r.lower()
        if r == 'k':
            found['Status'] = 'OK'
            results.append(found)
            answered = True
        elif r == 'o':
            found['Status'] = 'OLD'
            results.append(found)
            answered = True
        elif r == 'f':
            found['Status'] = 'FAIL'
            results.append(found)
            answered = True
        elif r == 'x':
            answered = True
    print()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Classify SMART data manually. Now.')
    parser.add_argument('files', nargs='+', type=str, help="Path to smartctl saved files")
    args = parser.parse_args()

    get_files(args.files)
