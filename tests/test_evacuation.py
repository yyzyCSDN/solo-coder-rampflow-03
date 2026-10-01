import unittest
from datetime import datetime,timezone

from rampflow.evacuation import(
    ApronEvacuation,MusterPoint,Person,PersonCategory,TeamReport,WalkIn,
    EvacuationNotReady,
)
from rampflow.models import Decision
from rampflow.workflows.control import RampWorkflowControl,WorkflowRejected

UTC=timezone.utc
D=lambda m:datetime(2026,9,30,10,m,tzinfo=UTC)

P=lambda pid,name,team,zone='zone-a',muster='M1',cat=PersonCategory.EMPLOYEE:Person(pid,name,cat,team,zone,muster)

class EvacuationTests(unittest.TestCase):
    def setUp(self):
        self.ev=ApronEvacuation('EV1',[MusterPoint('M1','North','zone-a'),MusterPoint('M2','South','zone-b')])
        for person in[
            P('E1','Alice','ramp-1'),
            P('E2','Bob','ramp-1'),
            Person('C1','Cara',PersonCategory.CONTRACTOR,'fueling','zone-a','M1'),
            Person('V1','Victor',PersonCategory.VISITOR,'auditors','zone-b','M2'),
        ]:self.ev.register_person(person)

    def test_roster_covers_contractors_and_visitors(self):
        by_cat=self.ev.counts_by_category()
        self.assertEqual((by_cat[PersonCategory.EMPLOYEE].expected,by_cat[PersonCategory.CONTRACTOR].expected,by_cat[PersonCategory.VISITOR].expected),(2,1,1))
        self.assertEqual(self.ev.headcount().missing,4)

    def test_duplicate_signins_count_once_within_and_across_teams(self):
        self.ev.submit_report(TeamReport('R1','ramp-1','M1',D(2),person_ids=('E1','E2','E1')))
        self.ev.submit_report(TeamReport('R2','fueling','M1',D(3),person_ids=('E1','C1')))
        hc=self.ev.headcount()
        self.assertEqual((hc.expected,hc.accounted,hc.missing),(4,3,1))
        self.assertEqual(self.ev.muster_counts()['M1'].present,3)

    def test_report_replay_is_idempotent(self):
        r1=self.ev.submit_report(TeamReport('R1','ramp-1','M1',D(2),person_ids=('E1','E2')))
        r2=self.ev.submit_report(TeamReport('R1','ramp-1','M1',D(2),person_ids=('E1','E2')))
        self.assertIs(r1,r2)
        self.assertEqual(self.ev.headcount().accounted,2)

    def test_relocation_to_other_muster_point_moves_count(self):
        self.ev.submit_report(TeamReport('R1','ramp-1','M1',D(2),person_ids=('E1','E2','C1')))
        acc=self.ev.submit_report(TeamReport('R2','ramp-1','M2',D(4),person_ids=('E1',)))
        self.assertEqual(acc.relocated,(('E1','M1','M2'),))
        counts=self.ev.muster_counts()
        self.assertEqual(counts['M1'].present,2)
        self.assertEqual(counts['M2'].present,1)
        self.assertEqual(counts['M1'].relocated_out,('E1',))
        self.assertEqual(counts['M2'].relocated_in,('E1',))
        status=self.ev.person_status('E1')
        self.assertTrue(status.relocated and status.actual_muster=='M2')
        self.assertEqual(self.ev.person_status('E1').person.work_zone,'zone-a')

    def test_unknown_scan_blocks_close_and_can_be_reconciled(self):
        self.ev.submit_report(TeamReport('R1','ramp-1','M1',D(2),person_ids=('E1','E2','C1')))
        self.ev.submit_report(TeamReport('R2','visits','M2',D(3),person_ids=('V1',),walk_ins=(WalkIn('w1','Temp Bob',PersonCategory.VISITOR,'Portable Co','B7788'),)))
        decision=self.ev.close_readiness()
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.code,'persons-unaccounted')
        self.assertIn('badge:B7788',decision.details['unmatched'])
        with self.assertRaises(EvacuationNotReady):self.ev.close(D(10))

        self.ev.confirm_extra_person(
            Person('X1','Temp Bob',PersonCategory.VISITOR,'Portable Co','gate','M2'),'badge:B7788')
        self.assertTrue(self.ev.close_readiness().accepted)
        closed=self.ev.close(D(10))
        self.assertEqual(closed,D(10))
        with self.assertRaises(RuntimeError):self.ev.redirect('E1','M2',D(11))

    def test_unmatched_scan_can_be_bound_to_existing_roster_person(self):
        self.ev.submit_report(TeamReport('R0','fueling','M1',D(2),person_ids=('E1','E2','C1','V1')))
        self.ev.submit_report(TeamReport('R1','fueling','M1',D(3),person_ids=('BADGE-42',)))
        self.assertIn('BADGE-42',self.ev.unmatched_checkins())
        self.ev.reconcile_checkin('BADGE-42','V1')
        self.assertEqual(self.ev.unmatched_checkins(),{})
        status=self.ev.person_status('V1')
        self.assertEqual(status.actual_muster,'M1')
        self.assertTrue(status.relocated)

    def test_bulk_headcount_is_provisional_until_people_align(self):
        # Team reports 4 present but only resolves 3 people.
        self.ev.submit_report(TeamReport('R1','ramp-1','M1',D(2),person_ids=('E1','E2','C1'),bulk_count=4))
        self.assertEqual(self.ev.muster_counts()['M1'].unverified_gap,1)
        self.assertFalse(self.ev.close_readiness().accepted)

        # Corrected report resolves the fourth person and supersedes the loose count.
        self.ev.submit_report(TeamReport('R2','ramp-1','M1',D(4),person_ids=('V1',),bulk_count=4,supersedes=('R1',)))
        decision=self.ev.close_readiness()
        self.assertTrue(decision.accepted,decision.reason)
        self.assertEqual(self.ev.headcount().accounted,4)

    def test_explicit_redirect_correction(self):
        self.ev.submit_report(TeamReport('R1','ramp-1','M1',D(2),person_ids=('E1','E2','C1','V1')))
        self.ev.redirect('V1','M2',D(5),'visitor redirected by marshal')
        self.assertEqual(self.ev.muster_counts()['M1'].present,3)
        self.assertEqual(self.ev.muster_counts()['M2'].present,1)

    def test_team_and_muster_views_stay_consistent(self):
        self.ev.submit_report(TeamReport('R1','ramp-1','M1',D(2),person_ids=('E1','E2')))
        teams=self.ev.counts_by_team()
        self.assertEqual((teams['ramp-1'].expected,teams['ramp-1'].accounted,teams['ramp-1'].missing),(2,2,0))
        self.assertEqual(teams['fueling'].missing,1)

    def test_capacity_warning_but_no_hard_block(self):
        ev=ApronEvacuation('EV2',[MusterPoint('M1','North','zone-a',capacity=1)])
        ev.register_person(P('E1','Alice','ramp-1'))
        ev.register_person(P('E2','Bob','ramp-1'))
        ev.submit_report(TeamReport('R1','ramp-1','M1',D(2),person_ids=('E1','E2')))
        decision=ev.close_readiness()
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.details['over_capacity'],('M1',))

    def test_workflow_close_publishes_only_after_people_aligned(self):
        control=RampWorkflowControl()
        self.ev.submit_report(TeamReport('R1','ramp-1','M1',D(2),person_ids=('E1','E2','C1')))
        with self.assertRaises(WorkflowRejected):
            control.close_apron_evacuation('op-1','EV1',D(9),lambda:self.ev.close(D(9)),requirements=(self.ev.close_readiness(),))
        self.assertEqual(control.stream.head,0)

        self.ev.submit_report(TeamReport('R2','visits','M2',D(4),person_ids=('V1',)))
        receipt=control.close_apron_evacuation('op-2','EV1',D(10),lambda:self.ev.close(D(10)),requirements=(self.ev.close_readiness(),))
        self.assertEqual(receipt.event.topic,'apron.evacuation.closed')
        self.assertEqual(control.stream.head,1)
