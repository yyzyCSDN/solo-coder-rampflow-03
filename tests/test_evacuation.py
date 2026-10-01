import unittest
from datetime import datetime,timezone

from rampflow.evacuation import (
    AccountState,EvacuationCoordinator,MusterPoint,Person,PersonCategory,
    CheckIn,HeadcountReport,OffsiteReason,
)

UTC=timezone.utc
T=lambda m=0:datetime(2026,9,30,10,m,tzinfo=UTC)

def coordinator_with_points():
    c=EvacuationCoordinator()
    c.register_point(MusterPoint('MP-A','A 区集合点',200))
    c.register_point(MusterPoint('MP-B','B 区集合点',120))
    return c

def person(pid,team='ramp-a',category=PersonCategory.EMPLOYEE,point='MP-A',**kw):
    return Person(pid,pid,category,team,'stand-12',point,**kw)


class EvacuationAccountabilityTests(unittest.TestCase):
    def test_roster_replaces_manual_piecing_and_maps_area_to_point(self):
        c=coordinator_with_points()
        c.open_event('EV-1',T())
        c.add_person(person('E1'))
        c.ingest_roster([
            person('C1',category=PersonCategory.CONTRACTOR,employer='航油外包'),
            person('V1',category=PersonCategory.VISITOR,escort_id='E1'),
        ])
        roll=c.roll_call()
        self.assertEqual(roll.expected_total,3)
        self.assertEqual(roll.accounted_total,0)
        self.assertEqual({r.person_id for r in roll.unaccounted},{'C1','E1','V1'})

    def test_duplicate_scans_count_person_once(self):
        c=coordinator_with_points()
        c.open_event('EV-1',T())
        c.add_person(person('E1'))
        first=c.check_in(CheckIn('S1','E1','MP-A',T(2)))
        replay=c.check_in(CheckIn('S1','E1','MP-A',T(2)))  # 同一条扫码重发
        c.check_in(CheckIn('S2','E1','MP-A',T(3)))          # 同一点重复签到
        self.assertEqual(first.state,AccountState.MUSTERED)
        self.assertEqual(replay.state,AccountState.MUSTERED)
        roll=c.roll_call()
        self.assertEqual(roll.accounted_total,1)
        self.assertEqual(roll.mustered_by_point,{'MP-A':1})
        self.assertTrue(c.can_close().accepted)

    def test_wrong_muster_point_is_flagged_redirected_and_counted_once(self):
        c=coordinator_with_points()
        c.open_event('EV-1',T())
        c.add_person(person('E1',point='MP-A'))
        row=c.check_in(CheckIn('S1','E1','MP-B',T(2)))     # 实际去了 B 点
        self.assertEqual(row.state,AccountState.REDIRECTED)
        self.assertTrue(row.redirected)
        self.assertEqual(row.actual_point,'MP-B')
        # 纠正：再扫回计划点，以最新位置为准，仍然只算一人
        corrected=c.check_in(CheckIn('S2','E1','MP-A',T(4)))
        self.assertEqual(corrected.state,AccountState.MUSTERED)
        self.assertEqual(c.roll_call().accounted_total,1)

    def test_unregistered_visitor_scan_blocks_close_then_adopts_on_registration(self):
        c=coordinator_with_points()
        c.open_event('EV-1',T())
        c.add_person(person('E1'))
        c.check_in(CheckIn('S1','E1','MP-A',T(2)))
        unknown=c.check_in(CheckIn('S9','V-X','MP-A',T(3)))  # 漏报名册的临时访客
        self.assertEqual(unknown.state,AccountState.UNREGISTERED)
        decision=c.can_close()
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.code,'unregistered-checkins')
        with self.assertRaises(RuntimeError):
            c.close_event(T(5))
        # 补录访客：已有签到自动认领，不需要重扫
        c.add_person(person('V-X',category=PersonCategory.VISITOR))
        roll=c.roll_call()
        self.assertEqual(roll.unregistered_scans,())
        self.assertEqual(roll.accounted_total,2)
        self.assertTrue(c.can_close().accepted)
        self.assertEqual(c.close_event(T(6)).closed_at,T(6))

    def test_missing_person_blocks_close(self):
        c=coordinator_with_points()
        c.open_event('EV-1',T())
        c.add_person(person('E1'));c.add_person(person('C1',category=PersonCategory.CONTRACTOR))
        c.check_in(CheckIn('S1','E1','MP-A',T(2)))
        decision=c.can_close()
        self.assertEqual(decision.code,'unaccounted-people')
        self.assertEqual(decision.details['person_ids'],['C1'])
        self.assertFalse(decision.accepted)

    def test_manual_headcount_never_counts_but_discrepancy_blocks_close(self):
        c=coordinator_with_points()
        c.open_event('EV-1',T())
        c.add_person(person('E1'));c.add_person(person('E2'))
        c.check_in(CheckIn('S1','E1','MP-A',T(2)))
        c.check_in(CheckIn('S2','E2','MP-A',T(2)))
        # 班组拍脑袋报 5 人（实名只有 2）：不能因此变成 5，且阻止关闭
        c.report_headcount(HeadcountReport('ramp-a','MP-A',5,T(3)))
        decision=c.can_close()
        self.assertEqual(decision.code,'headcount-mismatch')
        self.assertEqual(c.roll_call().accounted_total,2)
        c.reports.clear()
        c.report_headcount(HeadcountReport('ramp-a','MP-A',2,T(4)))
        self.assertTrue(c.can_close().accepted)

    def test_redirected_headcount_compares_against_actual_point(self):
        c=coordinator_with_points()
        c.open_event('EV-1',T())
        c.add_person(person('E1',point='MP-A'))
        c.check_in(CheckIn('S1','E1','MP-B',T(2)))
        # A 点班组报 1 人对不上（人实际在 B）；B 点报 1 人才对
        c.report_headcount(HeadcountReport('ramp-a','MP-A',1,T(3)))
        self.assertFalse(c.can_close().accepted)
        c.reports.clear()
        c.report_headcount(HeadcountReport('ramp-a','MP-B',1,T(3)))
        self.assertTrue(c.can_close().accepted)

    def test_offsite_resolution_and_scan_precedence(self):
        c=coordinator_with_points()
        c.open_event('EV-1',T())
        c.add_person(person('E1'));c.add_person(person('E2'))
        c.check_in(CheckIn('S1','E1','MP-A',T(2)))
        c.resolve_offsite('E2','duty-mgr',OffsiteReason.RELEASED,T(2))
        row=next(r for r in c.roll_call().rows if r.person_id=='E2')
        self.assertEqual(row.state,AccountState.OFFSITE)
        self.assertTrue(c.can_close().accepted)
        # 已签到的人不能被"标记离场"覆盖
        with self.assertRaises(RuntimeError):
            c.resolve_offsite('E1','duty-mgr',OffsiteReason.OTHER,T(3))

    def test_contractors_and_visitors_explicit_in_rollcall(self):
        c=coordinator_with_points()
        c.open_event('EV-1',T())
        c.ingest_roster([
            person('C1',category=PersonCategory.CONTRACTOR,employer='外包'),
            person('V1',category=PersonCategory.VISITOR,escort_id='C1'),
        ])
        c.check_in(CheckIn('S1','C1','MP-A',T(1)))
        c.check_in(CheckIn('S2','V1','MP-A',T(1)))
        roll=c.roll_call()
        cats={r.person_id:r.category for r in roll.rows}
        self.assertEqual(cats,{'C1':PersonCategory.CONTRACTOR,'V1':PersonCategory.VISITOR})
        self.assertTrue(roll.aligned)

    def test_scan_id_identity_conflict_rejected(self):
        c=coordinator_with_points()
        c.open_event('EV-1',T())
        c.add_person(person('E1'));c.add_person(person('E2'))
        c.check_in(CheckIn('S1','E1','MP-A',T(1)))
        with self.assertRaises(ValueError):
            c.check_in(CheckIn('S1','E2','MP-A',T(1)))

    def test_no_changes_after_close(self):
        c=coordinator_with_points()
        c.open_event('EV-1',T())
        c.add_person(person('E1'))
        c.check_in(CheckIn('S1','E1','MP-A',T(1)))
        c.close_event(T(2))
        with self.assertRaises(RuntimeError):
            c.check_in(CheckIn('S2','E1','MP-A',T(3)))
        with self.assertRaises(RuntimeError):
            c.add_person(person('E3'))


if __name__=='__main__':
    unittest.main()
