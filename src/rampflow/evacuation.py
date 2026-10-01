from __future__ import annotations
from dataclasses import dataclass,replace
from datetime import datetime
from enum import Enum
from typing import Iterable,Mapping
from .timeutil import utc
from .models import Decision

class PersonCategory(str,Enum):
    EMPLOYEE='employee';CONTRACTOR='contractor';VISITOR='visitor'

class AccountState(str,Enum):
    UNACCOUNTED='unaccounted';MUSTERED='mustered';REDIRECTED='redirected';OFFSITE='offsite';UNREGISTERED='unregistered'

class OffsiteReason(str,Enum):
    RELEASED='released'        # 疏散前已离场/休假
    MEDICAL='medical'          # 送医
    SECONDARY='secondary'      # 核实后确认在其他受控点位
    OTHER='other'

class EvacuationPhase(str,Enum):
    ACTIVE='active';CLOSED='closed'

@dataclass(frozen=True)
class MusterPoint:
    point_id:str
    name:str
    capacity:int=0
    def __post_init__(self):
        if not self.point_id:raise ValueError('point id required')
        if self.capacity<0:raise ValueError('negative capacity')

@dataclass(frozen=True)
class Person:
    person_id:str
    name:str
    category:PersonCategory
    team:str
    home_area:str                       # 原作业区
    assigned_point:str                 # 计划集合点
    employer:str=''                     # 承包商单位（访客为接待方）
    expected:bool=True                  # 是否应在机坪（休假/离场可置 False）
    escort_id:str|None=None             # 临时访客的陪同人

@dataclass(frozen=True)
class CheckIn:
    scan_id:str
    person_id:str
    point_id:str
    at:datetime
    method:str='scan'
    def __post_init__(self):object.__setattr__(self,'at',utc(self.at))

@dataclass(frozen=True)
class HeadcountReport:
    """班组口头/手工报数：只作核对线索，永远不参与实名计数。"""
    team:str
    point_id:str
    counted:int
    at:datetime
    note:str=''
    def __post_init__(self):
        object.__setattr__(self,'at',utc(self.at))
        if self.counted<0:raise ValueError('negative headcount')

@dataclass(frozen=True)
class AccountRow:
    person_id:str
    name:str
    category:PersonCategory
    team:str
    home_area:str
    assigned_point:str
    actual_point:str|None
    state:AccountState
    redirected:bool

@dataclass(frozen=True)
class EvacuationEvent:
    event_id:str
    opened_at:datetime
    closed_at:datetime|None
    phase:EvacuationPhase
    version:int=1
    def __post_init__(self):object.__setattr__(self,'opened_at',utc(self.opened_at))

@dataclass(frozen=True)
class RollCall:
    rows:tuple[AccountRow,...]
    expected_total:int
    accounted_total:int
    mustered_by_point:Mapping[str,int]
    unaccounted:tuple[AccountRow,...]
    unregistered_scans:tuple[CheckIn,...]
    report_discrepancies:tuple[Mapping[str,object],...]
    @property
    def aligned(self)->bool:
        return not self.unaccounted and not self.unregistered_scans

def _row_for(person:Person,latest:CheckIn|None,resolved:Mapping[str,tuple[str,OffsiteReason]])->AccountRow:
    if person.person_id in resolved:
        return AccountRow(person.person_id,person.name,person.category,person.team,person.home_area,
                          person.assigned_point,None,AccountState.OFFSITE,False)
    if latest is None:
        return AccountRow(person.person_id,person.name,person.category,person.team,person.home_area,
                          person.assigned_point,None,AccountState.UNACCOUNTED,False)
    redirected=latest.point_id!=person.assigned_point
    state=AccountState.REDIRECTED if redirected else AccountState.MUSTERED
    return AccountRow(person.person_id,person.name,person.category,person.team,person.home_area,
                      person.assigned_point,latest.point_id,state,redirected)

class EvacuationCoordinator:
    """机坪疏散清点：名册、作业区-集合点对应、签到去重与关闭闸门。"""

    def __init__(self):
        self.points:dict[str,MusterPoint]={}
        self.people:dict[str,Person]={}
        self.event:EvacuationEvent|None=None
        self._scans:dict[str,CheckIn]={}          # scan_id 去重
        self.history:dict[str,list[CheckIn]]={}   # person_id 全量轨迹
        self._unregistered:dict[str,CheckIn]={}   # scan_id -> 名册外签到
        self.reports:list[HeadcountReport]=[]
        self._resolved:dict[str,tuple[str,OffsiteReason]]={}  # person_id -> (by, reason)

    # ---- 基础数据 ----
    def register_point(self,point:MusterPoint)->None:
        if point.point_id in self.points:raise ValueError('duplicate muster point')
        self.points[point.point_id]=point

    def add_person(self,person:Person)->None:
        if self.event is not None and self.event.phase==EvacuationPhase.CLOSED:
            raise RuntimeError('event closed')
        if person.person_id in self.people:raise ValueError('duplicate person')
        if person.assigned_point not in self.points:
            raise ValueError(f"unknown assigned point {person.assigned_point}")
        self.people[person.person_id]=person
        # 签到先于补录到达（典型漏报承包商/访客）：补录后自动认领，无需重扫
        pending=[s for s in self._unregistered.values() if s.person_id==person.person_id]
        if pending:
            adopted=self.history.setdefault(person.person_id,[])
            adopted.extend(sorted(pending,key=lambda s:(s.at,s.scan_id)))
            for scan in pending:
                self._scans[scan.scan_id]=scan
                del self._unregistered[scan.scan_id]
        if self.event is not None:
            self.event=replace(self.event,version=self.event.version+1)

    def ingest_roster(self,people:Iterable[Person])->int:
        """批量导入班组/承包商/访客名册，返回新增人数。"""
        added=0
        for person in people:
            self.add_person(person);added+=1
        return added

    # ---- 事件生命周期 ----
    def open_event(self,event_id:str,at:datetime)->EvacuationEvent:
        if self.event is not None and self.event.phase==EvacuationPhase.ACTIVE:
            raise RuntimeError('an evacuation event is already active')
        event=EvacuationEvent(event_id,utc(at),None,EvacuationPhase.ACTIVE)
        self.event=event
        return event

    def _require_active(self)->EvacuationEvent:
        if self.event is None:raise RuntimeError('no active evacuation event')
        if self.event.phase==EvacuationPhase.CLOSED:raise RuntimeError('event closed')
        return self.event

    # ---- 签到 ----
    def check_in(self,scan:CheckIn)->AccountRow:
        """幂等签到：重复 scan_id 直接回放；同一人重复/换点以最新一次为准，不多算。"""
        event=self._require_active()
        previous=self._scans.get(scan.scan_id) or self._unregistered.get(scan.scan_id)
        if previous is not None:
            if previous.person_id!=scan.person_id or previous.point_id!=scan.point_id:
                raise ValueError('scan id reused with different identity or point')
            if scan.person_id in self.people:
                return self._row(scan.person_id)
            return AccountRow(scan.person_id,'(unregistered)',PersonCategory.VISITOR,'','',
                              scan.point_id,scan.point_id,AccountState.UNREGISTERED,False)
        if scan.point_id not in self.points:raise ValueError(f"unknown muster point {scan.point_id}")
        person=self.people.get(scan.person_id)
        if person is None:
            # 名册外（典型：临时访客、漏报承包商）先挂起，阻止关闭，待补录后自动对齐
            self._unregistered[scan.scan_id]=scan
            self.event=replace(event,version=event.version+1)
            return AccountRow(scan.person_id,'(unregistered)',PersonCategory.VISITOR,'','',
                              scan.point_id,scan.point_id,AccountState.UNREGISTERED,False)
        rows=self.history.setdefault(scan.person_id,[])
        if rows and scan.at<rows[-1].at:raise ValueError('out-of-order check-in')
        rows.append(scan)
        self._scans[scan.scan_id]=scan
        self.event=replace(event,version=event.version+1)
        return self._row(scan.person_id)

    def resolve_offsite(self,person_id:str,by:str,reason:OffsiteReason,at:datetime)->AccountRow:
        """登记应到但不会到集合点的人（已离场/送医等）；已有签到者以签到为准，不可覆盖。"""
        event=self._require_active()
        if person_id not in self.people:raise KeyError(person_id)
        if self.history.get(person_id):raise RuntimeError('person already mustered; scan takes precedence')
        self._resolved[person_id]=(by,reason)
        self.event=replace(event,version=event.version+1)
        return self._row(person_id)

    def report_headcount(self,report:HeadcountReport)->None:
        """班组手工报数，仅与实名结果核对，绝不计入 accounted。"""
        self._require_active()
        if report.point_id not in self.points:raise ValueError('unknown muster point')
        self.reports.append(report)

    # ---- 清点视图 ----
    def _row(self,person_id:str)->AccountRow:
        history=self.history.get(person_id)
        return _row_for(self.people[person_id],history[-1] if history else None,self._resolved)

    def roll_call(self)->RollCall:
        rows=tuple(self._row(pid) for pid in sorted(self.people))
        expected=[r for r in rows if self.people[r.person_id].expected]
        accounted={}
        for r in expected:
            if r.actual_point is not None:
                accounted[r.person_id]=r.actual_point
        by_point:dict[str,int]={}
        for point in accounted.values():by_point[point]=by_point.get(point,0)+1
        unaccounted=tuple(r for r in expected
                          if r.state==AccountState.UNACCOUNTED)
        discrepancies=self._report_discrepancies(rows)
        return RollCall(rows,len(expected),len(accounted),dict(sorted(by_point.items())),
                        unaccounted,tuple(sorted(self._unregistered.values(),key=lambda s:s.scan_id)),
                        tuple(discrepancies))

    def _report_discrepancies(self,rows:tuple[AccountRow,...])->list[Mapping[str,object]]:
        actual:dict[tuple[str,str],int]={}
        for r in rows:
            if not self.people[r.person_id].expected or not r.actual_point:continue
            actual[(r.team,r.actual_point)]=actual.get((r.team,r.actual_point),0)+1
        out=[]
        for rep in self.reports:
            real=actual.get((rep.team,rep.point_id),0)
            if real!=rep.counted:
                out.append({'team':rep.team,'point_id':rep.point_id,'reported':rep.counted,
                            'identified':real,'delta':rep.counted-real})
        return out

    def missing(self)->tuple[AccountRow,...]:
        return self.roll_call().unaccounted

    # ---- 关闭闸门 ----
    def can_close(self)->Decision:
        if self.event is None:return Decision(False,'no-event','no evacuation event opened')
        if self.event.phase==EvacuationPhase.CLOSED:return Decision(True,'already-closed','event already closed')
        roll=self.roll_call()
        if roll.unregistered_scans:
            ids=sorted({s.person_id for s in roll.unregistered_scans})
            return Decision(False,'unregistered-checkins',
                            'check-ins with no roster entry; register or verify them first',
                            {'person_ids':ids})
        if roll.unaccounted:
            ids=sorted(r.person_id for r in roll.unaccounted)
            return Decision(False,'unaccounted-people','people still missing on the apron',
                            {'person_ids':ids})
        if roll.report_discrepancies:
            return Decision(False,'headcount-mismatch',
                            'manual team counts do not match identified people',
                            {'discrepancies':list(roll.report_discrepancies)})
        return Decision(True,'aligned','every expected person accounted for exactly once')

    def close_event(self,at:datetime)->EvacuationEvent:
        decision=self.can_close()
        if not decision.accepted:raise RuntimeError(f"cannot close: {decision.code}")
        if self.event.phase==EvacuationPhase.CLOSED:return self.event
        closed=replace(self.event,phase=EvacuationPhase.CLOSED,closed_at=utc(at),version=self.event.version+1)
        self.event=closed
        return closed
