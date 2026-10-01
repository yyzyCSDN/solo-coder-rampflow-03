from __future__ import annotations
from dataclasses import dataclass,field
from datetime import datetime
from enum import Enum
from typing import Iterable,Mapping
from .timeutil import utc
from .models import Decision

class PersonCategory(str,Enum):
    EMPLOYEE='employee';CONTRACTOR='contractor';VISITOR='visitor'

class EvacuationPhase(str,Enum):
    OPEN='open';CLOSED='closed'

@dataclass(frozen=True)
class MusterPoint:
    point_id:str
    label:str
    serving_zone:str=''
    capacity:int|None=None
    def __post_init__(self):
        if not self.point_id:raise ValueError('muster point id required')
        if self.capacity is not None and self.capacity<0:raise ValueError('negative muster capacity')

@dataclass(frozen=True)
class Person:
    person_id:str
    name:str
    category:PersonCategory
    team:str
    work_zone:str
    assigned_muster:str
    def __post_init__(self):
        if not self.person_id or not self.name:raise ValueError('person id and name required')
        if not self.team or not self.work_zone or not self.assigned_muster:raise ValueError('team, work zone and assigned muster required')

@dataclass(frozen=True)
class WalkIn:
    """A person who arrives at a muster point without a known roster id (contractor/visitor badge)."""
    ref:str
    name:str
    category:PersonCategory
    organization:str=''
    badge:str=''
    def __post_init__(self):
        if not self.ref or not self.name:raise ValueError('walk-in ref and name required')

@dataclass(frozen=True)
class TeamReport:
    report_id:str
    team:str
    muster_point:str
    at:datetime
    person_ids:tuple[str,...]=()
    walk_ins:tuple[WalkIn,...]=()
    bulk_count:int|None=None
    supersedes:tuple[str,...]=()
    def __post_init__(self):
        object.__setattr__(self,'at',utc(self.at))
        if not self.report_id or not self.team:raise ValueError('report id and team required')
        if isinstance(self.person_ids,str) or any(not isinstance(pid,str) for pid in self.person_ids):raise ValueError('person_ids must be a sequence of person id strings')
        if isinstance(self.walk_ins,WalkIn):raise ValueError('walk_ins must be a sequence, not a single WalkIn')
        if self.bulk_count is not None and self.bulk_count<0:raise ValueError('negative bulk count')

@dataclass(frozen=True)
class ReportAcceptance:
    report_id:str
    muster_point:str
    accepted:tuple[str,...]
    duplicates:tuple[str,...]
    relocated:tuple[tuple[str,str,str],...]
    created_unmatched:tuple[str,...]
    superseded:tuple[str,...]
    unverified_gap:int

@dataclass(frozen=True)
class PersonStatus:
    person:Person
    actual_muster:str|None
    first_seen:datetime|None
    last_seen:datetime|None
    relocated:bool
    @property
    def accounted(self)->bool:return self.actual_muster is not None

@dataclass(frozen=True)
class UnmatchedCheckIn:
    key:str
    name:str
    category:PersonCategory|None
    organization:str
    muster_point:str
    at:datetime
    source_team:str

@dataclass(frozen=True)
class MusterCount:
    point:MusterPoint
    assigned:int
    present:int
    relocated_in:tuple[str,...]
    relocated_out:tuple[str,...]
    unmatched_present:int
    unverified_gap:int

@dataclass(frozen=True)
class GroupCount:
    expected:int
    accounted:int
    missing:int

@dataclass(frozen=True)
class Headcount:
    expected:int
    accounted:int
    missing:int
    unmatched:int
    unverified:int

@dataclass
class _Account:
    actual_muster:str
    first_at:datetime
    last_at:datetime
    source_team:str
    report_id:str

class EvacuationNotReady(RuntimeError):
    def __init__(self,decision:Decision):
        self.decision=decision
        super().__init__(f'{decision.code}: {decision.reason}')

class ApronEvacuation:
    """Person-level muster reconciliation for one apron evacuation event.

    Head counts reported by teams are only trusted once they resolve to
    individual people.  Contractors and visitors live in the same roster as
    employees, duplicate scans never inflate the total, physical presence at
    another muster point relocates the person, and the event cannot close while
    any person or check-in remains unaligned.
    """
    def __init__(self,event_id:str,points:Iterable[MusterPoint]=()):
        if not event_id:raise ValueError('event id required')
        points=list(points)
        self.event_id=event_id
        self.phase=EvacuationPhase.OPEN
        self.version=1
        self.closed_at:datetime|None=None
        self.points:dict[str,MusterPoint]={}
        for p in points:self._add_point(p)
        self.roster:dict[str,Person]={}
        self._accounts:dict[str,_Account]={}
        self._unmatched:set[str]=set()
        self._walkin:dict[str,WalkIn]={}
        self._reports:dict[str,ReportAcceptance]={}
        self._bulk:dict[str,tuple[int,str]]={}

    def _bulk_gaps(self)->dict[str,tuple[int,str]]:
        """Unaligned remainder of every bulk count, measured against rostered people currently present."""
        present_by_point={pid:0 for pid in self.points}
        for pid,p in self.roster.items():
            acc=self._accounts.get(pid)
            if acc is not None:present_by_point[acc.actual_muster]=present_by_point.get(acc.actual_muster,0)+1
        gaps={}
        for rid,(bulk,point) in self._bulk.items():
            gap=bulk-present_by_point.get(point,0)
            if gap>0:gaps[rid]=(gap,point)
        return gaps

    def _add_point(self,point:MusterPoint)->None:
        if point.point_id in self.points:raise ValueError(f'duplicate muster point {point.point_id}')
        self.points[point.point_id]=point

    def add_muster_point(self,point:MusterPoint)->None:
        self._require_open()
        self._add_point(point)
        self.version+=1

    def _require_open(self)->None:
        if self.phase is EvacuationPhase.CLOSED:raise RuntimeError('evacuation event closed')

    def _require_point(self,point_id:str)->MusterPoint:
        point=self.points.get(point_id)
        if point is None:raise ValueError(f'unknown muster point {point_id}')
        return point

    def register_person(self,person:Person)->None:
        self._require_open()
        if person.person_id in self.roster:raise ValueError(f'duplicate person {person.person_id}')
        self._require_point(person.assigned_muster)
        self.roster[person.person_id]=person
        # A roster correction may land after the person already scanned as unmatched.
        if person.person_id in self._unmatched:
            self._adopt_unmatched(person.person_id,person.person_id)
        self.version+=1

    def _adopt_unmatched(self,raw_key:str,person_id:str)->None:
        acc=self._accounts.pop(raw_key)
        existing=self._accounts.get(person_id)
        if existing is None:
            self._accounts[person_id]=acc
        else:
            later,earlier=(acc,existing) if acc.last_at>=existing.last_at else (existing,acc)
            existing.actual_muster=later.actual_muster
            existing.first_at=min(existing.first_at,acc.first_at)
            existing.last_at=later.last_at
            existing.source_team=later.source_team
            existing.report_id=later.report_id
        self._unmatched.discard(raw_key)
        self._walkin.pop(raw_key,None)

    def reconcile_checkin(self,raw_key:str,person_id:str)->None:
        """Map an unrecognized scan/badge to a person already on the roster."""
        self._require_open()
        if raw_key not in self._unmatched:raise ValueError(f'{raw_key} is not an unmatched check-in')
        if person_id not in self.roster:raise ValueError(f'unknown person {person_id}')
        if person_id in self._unmatched:raise ValueError('target person is itself unmatched')
        self._adopt_unmatched(raw_key,person_id)
        self.version+=1

    def confirm_extra_person(self,person:Person,raw_key:str|None=None)->None:
        """Register a legitimate late joiner (e.g. unplanned contractor) and optionally bind their scan."""
        self._require_open()
        if person.person_id in self.roster:raise ValueError(f'duplicate person {person.person_id}')
        self._require_point(person.assigned_muster)
        if raw_key is not None and raw_key not in self._unmatched:raise ValueError(f'{raw_key} is not an unmatched check-in')
        self.roster[person.person_id]=person
        if raw_key is not None:self._adopt_unmatched(raw_key,person.person_id)
        self.version+=1

    def _walkin_key(self,w:WalkIn,report_id:str)->str:
        return f'badge:{w.badge}' if w.badge else f'walkin:{report_id}:{w.ref}'

    def submit_report(self,report:TeamReport)->ReportAcceptance:
        self._require_open()
        previous=self._reports.get(report.report_id)
        if previous is not None:return previous
        point=self._require_point(report.muster_point)
        for old in report.supersedes:
            if old not in self._reports:raise ValueError(f'cannot supersede unknown report {old}')
        accepted:list[str]=[];duplicates:list[str]=[];relocated:list[tuple[str,str,str]]=[];created:list[str]=[]
        seen:set[str]=set()
        keys=[(pid,False,None) for pid in report.person_ids]
        keys += [(self._walkin_key(w,report.report_id),True,w) for w in report.walk_ins]
        for key,is_walkin,w in keys:
            if key in seen:
                duplicates.append(key);continue
            seen.add(key)
            acc=self._accounts.get(key)
            if acc is None:
                if is_walkin:
                    self._walkin.setdefault(key,w)
                    if key not in self.roster:self._unmatched.add(key);created.append(key)
                elif key not in self.roster:
                    self._unmatched.add(key);created.append(key)
                self._accounts[key]=_Account(point.point_id,report.at,report.at,report.team,report.report_id)
                accepted.append(key)
                continue
            if report.at<acc.last_at:raise ValueError('out-of-order check-in')
            if acc.actual_muster==point.point_id:
                duplicates.append(key)
            else:
                relocated.append((key,acc.actual_muster,point.point_id))
                acc.actual_muster=point.point_id
                acc.last_at=report.at
                acc.source_team=report.team
                acc.report_id=report.report_id
        superseded:list[str]=[]
        for old in report.supersedes:
            self._bulk.pop(old,None);superseded.append(old)
        if report.bulk_count is not None:
            self._bulk[report.report_id]=(report.bulk_count,point.point_id)
        gap=self._bulk_gaps().get(report.report_id,(0,point.point_id))[0]
        result=ReportAcceptance(report.report_id,point.point_id,tuple(accepted),tuple(duplicates),tuple(relocated),tuple(created),tuple(superseded),gap)
        self._reports[report.report_id]=result
        self.version+=1
        return result

    def redirect(self,key:str,to_point:str,at:datetime,reason:str='')->None:
        """Explicit correction when a person is confirmed at a different muster point."""
        self._require_open()
        at=utc(at)
        point=self._require_point(to_point)
        acc=self._accounts.get(key)
        if acc is None:raise ValueError(f'no check-in for {key}')
        if at<acc.last_at:raise ValueError('out-of-order check-in')
        if acc.actual_muster!=point.point_id:acc.actual_muster=point.point_id
        acc.last_at=at;acc.source_team='manual-correction';acc.report_id=f'redirect:{reason or key}'
        self.version+=1

    def person_status(self,person_id:str)->PersonStatus:
        person=self.roster[person_id]
        acc=self._accounts.get(person_id)
        if acc is None:return PersonStatus(person,None,None,None,False)
        return PersonStatus(person,acc.actual_muster,acc.first_at,acc.last_at,acc.actual_muster!=person.assigned_muster)

    def missing_persons(self)->tuple[Person,...]:
        return tuple(p for pid,p in sorted(self.roster.items()) if pid not in self._accounts)

    def unmatched_checkins(self)->Mapping[str,UnmatchedCheckIn]:
        out={}
        for key in sorted(self._unmatched):
            acc=self._accounts[key];w=self._walkin.get(key)
            out[key]=UnmatchedCheckIn(key,w.name if w else key,w.category if w else None,w.organization if w else '',acc.actual_muster,acc.last_at,acc.source_team)
        return out

    def muster_counts(self)->Mapping[str,MusterCount]:
        out={}
        for pid,point in self.points.items():
            assigned=sum(1 for p in self.roster.values() if p.assigned_muster==pid)
            present_keys=[k for k,p in self.roster.items() if (a:=self._accounts.get(k)) is not None and a.actual_muster==pid]
            rel_in=tuple(sorted(k for k in present_keys if self.roster[k].assigned_muster!=pid))
            rel_out=tuple(sorted(k for k,p in self.roster.items() if p.assigned_muster==pid and (a:=self._accounts.get(k)) is not None and a.actual_muster!=pid))
            unmatched=sum(1 for k in self._unmatched if self._accounts[k].actual_muster==pid)
            unverified=sum(g for g,mp in self._bulk_gaps().values() if mp==pid)
            out[pid]=MusterCount(point,assigned,len(present_keys),rel_in,rel_out,unmatched,unverified)
        return out

    def _group_count(self,key)->GroupCount:
        members=[p for p in self.roster.values() if key(p)]
        accounted=sum(1 for p in members if p.person_id in self._accounts)
        return GroupCount(len(members),accounted,len(members)-accounted)

    def counts_by_category(self)->Mapping[PersonCategory,GroupCount]:
        return {c:self._group_count(lambda p,c=c:p.category==c) for c in PersonCategory}

    def counts_by_team(self)->Mapping[str,GroupCount]:
        return {t:self._group_count(lambda p,t=t:p.team==t) for t in sorted({p.team for p in self.roster.values()})}

    def headcount(self)->Headcount:
        expected=len(self.roster)
        accounted=sum(1 for pid in self.roster if pid in self._accounts)
        gaps=self._bulk_gaps()
        return Headcount(expected,accounted,expected-accounted,len(self._unmatched),sum(g for g,_ in gaps.values()))

    def close_readiness(self)->Decision:
        hc=self.headcount()
        missing=tuple(p.person_id for p in self.missing_persons())
        unmatched=tuple(sorted(self._unmatched))
        unverified=tuple(sorted(self._bulk_gaps()))
        over_capacity=tuple(mp for mp,c in self.muster_counts().items() if c.point.capacity is not None and c.present+c.unmatched_present>c.point.capacity)
        details={'expected':hc.expected,'accounted':hc.accounted,'missing':missing,'unmatched':unmatched,'unverified_reports':unverified,'over_capacity':over_capacity}
        if missing or unmatched or unverified:
            return Decision(False,'persons-unaccounted','roster, scans or bulk reports are not aligned to people',details)
        return Decision(True,'all-accounted','every person aligned to a muster point',details)

    def close(self,at:datetime)->datetime:
        self._require_open()
        decision=self.close_readiness()
        if not decision.accepted:raise EvacuationNotReady(decision)
        self.phase=EvacuationPhase.CLOSED
        self.closed_at=utc(at)
        self.version+=1
        return self.closed_at
