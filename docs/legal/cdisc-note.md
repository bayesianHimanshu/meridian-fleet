# Courtesy note to CDISC (draft, not sent)

A draft for the project owner to send, or not. Nothing has been sent. Address: CDISC's
general contact (cdisc.org/contact); the owner chooses the address and sends it from their
own account.

---

**Subject:** Use of the CDISC SDTM/ADaM Pilot Project data in an open-source learning project

Hello,

I am writing as a courtesy to tell you how a small open-source project uses the CDISC
SDTM/ADaM Pilot Project data (CDISCPILOT01) from github.com/cdisc-org/sdtm-adam-pilot-project.

*Meridian Agent Fleet* is a learning project about clinical-trial oversight software. It
is set at a fictional company, and it is released under the Apache License 2.0. Its script
downloads the pilot's SDTM files from your repository; the project does not redistribute
them. It then derives a study it calls "MRD-201": the pilot's data relabelled, with
synthetic operational, safety and Trial Master File data added. Summary values from that
derived study appear in the project's documentation, its tests and a narrated demo video,
for example per-site counts and a few subject identifiers.

Wherever the derived study appears, we state: "Study MRD-201 is derived from the CDISC
SDTM/ADaM Pilot Project data (CDISCPILOT01), relabelled and with synthetic additions, used
under CDISC's terms of use. It is not a real study, and this project is not affiliated with
or endorsed by CDISC." The project's licence notice also says that the Apache License does
not cover the CDISC data or values derived from it. No fee is charged for anything.

If you would like us to change how the data is used or attributed, or to stop using it,
please let me know and I will do so.

Kind regards,
Himanshu
(repository: https://github.com/bayesianHimanshu/meridian-fleet)
