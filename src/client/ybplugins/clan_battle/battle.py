import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urljoin

import peewee
from aiocqhttp.api import Api
from apscheduler.triggers.cron import CronTrigger
from expiringdict import ExpiringDict
from quart import Quart, jsonify, redirect, request, session, url_for

from ..templating import render_template
from ..web_util import async_cached_func
from ..ybdata import (Clan_challenge, Clan_group, Clan_member, Clan_subscribe,
                      User)
from .exception import GroupError, InputError, UserError
from .typing import BossStatus, ClanBattleReport, Groupid, Pcr_date, QQid
from .util import atqq, pcr_datetime, pcr_timestamp, timed_cached_func

_logger = logging.getLogger(__name__)


class ClanBattle:
    Passive = True
    Active = True
    Request = True

    Commands = {
        '创建': 1,
        '加入': 2,
        '状态': 3,
        '报刀': 4,
        '尾刀': 5,
        '撤销': 6,
        '修正': 7,
        '修改': 7,
        '选择': 8,
        '切换': 8,
        '报告': 9,
        '查刀': 9,
        '预约': 10,
        '挂树': 11,
        '申请': 12,
        '取消': 13,
        '解锁': 14,
        '面板': 15,
        '后台': 15,
    }

    Server = {
        '日': 'jp',
        '台': 'tw',
        '韩': 'kr',
        '国': 'cn',
    }

    def __init__(self,
                 glo_setting: Dict[str, Any],
                 bot_api: Api,
                 *args, **kwargs):
        self.mode_on = (glo_setting["clan_battle_mode"] == "web")
        self.setting = glo_setting
        self.bossinfo = glo_setting['boss']
        self.api = bot_api

        # log
        if not os.path.exists(os.path.join(glo_setting['dirname'], 'log')):
            os.mkdir(os.path.join(glo_setting['dirname'], 'log'))

        formater = logging.Formatter(
            '[%(asctime)s] %(levelname)s: %(message)s')
        filehandler = logging.FileHandler(
            os.path.join(glo_setting['dirname'], 'log', '公会战日志.log'),
            encoding='utf-8',
        )
        filehandler.setFormatter(formater)
        filehandler.setLevel('INFO')
        _logger.addHandler(filehandler)

        # data initialize
        self._boss_status: Dict[str, asyncio.Future] = {}
        self._group_data: Dict[str, Clan_group] = {}
        self._report_cache = ExpiringDict(max_len=64, max_age_seconds=60)

        for group in Clan_group.select():
            self._group_data[group.group_id] = group
            self._boss_status[group.group_id] = asyncio.Future()

    def _level_by_cycle(self, cycle, level_4=False):
        if cycle <= 3:
            return 0
        elif cycle <= 10:
            return 1
        else:
            if level_4 and cycle >= 35:
                return 3
            return 2

    @timed_cached_func(128, 3600, ignore_self=True)
    def _get_nickname_by_qqid(self, qqid) -> Union[str, None]:
        user = User.get_or_create(qqid=qqid)[0]
        return user.nickname

    def _get_previous_challenge(self, *, qqid=None, group_id=None):
        expressions = []
        if qqid is not None:
            expressions.append(Clan_challenge.qqid == qqid)
        if group_id is not None:
            expressions.append(Clan_challenge.gid == group_id)
        if not expressions:
            raise ValueError('missing Parameter')

        try:
            lc = Clan_challenge.select(
                peewee.fn.MAX(Clan_challenge.cid)
            ).where(
                *expressions,
            ).scalar()
            return Clan_challenge.get_by_id(lc)
        except peewee.DoesNotExist:
            return None

    async def _update_group_list_async(self):
        try:
            group_list = await self.api.get_group_list()
        except Exception as e:
            _logger.error('获取群列表错误'+str(e))
            return False
        for group_info in group_list:
            group = Clan_group.get_or_none(
                group_id=group_info['group_id'],
            )
            if group is None:
                continue
            group.group_name = group_info['group_name']
            group.save()
            self._group_data[group_list] = group
        return True

    @async_cached_func(16)
    async def _fetch_member_list_async(self, group_id):
        try:
            group_member_list = await self.api.get_group_member_list(group_id=group_id)
        except Exception as e:
            _logger.error('获取群成员列表错误'+str(e))
            return []
        return group_member_list

    async def _update_all_group_members_async(self, group_id):
        group_member_list = await self._fetch_member_list_async(group_id)
        for member in group_member_list:
            user = User.get_or_create(qqid=member['user_id'])[0]
            membership = Clan_member.get_or_create(
                group_id=group_id, qqid=member['user_id'])[0]
            user.nickname = member.get('card') or member['nickname']
            user.clan_group_id = group_id
            if user.authority_group >= 10:
                user.authority_group = (
                    100 if member['role'] == 'member' else 10)
                membership.role = user.authority_group
            user.save()
            membership.save()

    def creat_group(self, group_id, game_server, group_name=None) -> None:
        """
        create a group for clan-battle

        Args:
            group_id: group id
            game_server: name of game server("jp" "tw" "cn" "kr")
        """
        group = Clan_group.get_or_none(group_id=group_id)
        if group is not None:
            raise GroupError('群已经存在')
        group = Clan_group.create(
            group_id=group_id,
            group_name=group_name,
            game_server=game_server,
            boss_health=self.bossinfo[game_server][0][0],
        )
        self._group_data[group_id] = group
        self._boss_status[group_id] = asyncio.Future()

    def bind_group(self, group_id, qqid) -> None:
        """
        set user's default group

        Args:
            group_id: group id
            qqid: qqid
        """
        user = User.get_or_create(qqid=qqid)[0]
        user.clan_group_id = group_id
        membership = Clan_member.get_or_create(
            group_id=group_id,
            qqid=qqid,
        )[0]
        user.save()

    def drop_member(self, group_id, member_list):
        """
        delete members from group member list

        permission should be checked before this function is called.

        Args:
            group_id: group id
            member_list: a list of qqid to delete
        """
        delete_count = Clan_member.delete().where(
            Clan_member.qqid.in_(member_list)
        ).execute()
        return delete_count

    def boss_status_summary(self, group_id) -> str:
        """
        get a summary of boss status

        Args:
            group_id: group id
        """
        group = self._group_data.get(group_id)
        if group is None:
            raise GroupError('本群未初始化')
        boss_summary = (
            f'现在{group.boss_cycle}周目，{group.boss_num}号boss\n'
            f'生命值{group.boss_health}'
        )
        if group.challenging_member_qq_id is not None:
            boss_summary += '\n{}正在挑战boss'.format(
                self._get_nickname_by_qqid(group.challenging_member_qq_id)
                or group.challenging_member_qq_id
            )
        return boss_summary

    def damage(self, group_id, qqid, damage, behalfed=None, comment={}) -> BossStatus:
        """
        record a non-defeat challenge to boss

        Args:
            group_id: group id
            qqid: qqid of member who do the record
            damage: the damage dealt to boss
            behalfed: the real member who did the challenge
            comment: extra infomation about the challenge
        """
        if damage < 0:
            raise InputError('伤害不可以是负数')
        group = self._group_data.get(group_id)
        if group is None:
            raise GroupError('本群未初始化')
        if damage >= group.boss_health:
            raise InputError('伤害超出剩余血量，如击败请使用尾刀')
        if behalfed is not None:
            nik = self._get_nickname_by_qqid(qqid) or qqid
            comment['behalf'] = f'由{nik}代报。'
            qqid = behalfed
        user = User.get_or_create(
            qqid=qqid,
            defaults={
                'clan_group_id': group_id,
            }
        )[0]
        d, t = pcr_datetime(area=group.game_server)
        challenges = Clan_challenge.select().where(
            Clan_challenge.gid == group_id,
            Clan_challenge.qqid == qqid,
            Clan_challenge.challenge_pcrdate == d,
        ).order_by(Clan_challenge.cid)
        challenges = list(challenges)
        if sum((not c.is_continue) for c in challenges) >= 3:
            raise InputError('今日上报次数已达到3次')
        is_continue = (challenges
                       and challenges[-1].boss_health_ramain == 0
                       and not challenges[-1].is_continue)
        challenge = Clan_challenge.create(
            gid=group_id,
            qqid=user.qqid,
            challenge_pcrdate=d,
            challenge_pcrtime=t,
            boss_cycle=group.boss_cycle,
            boss_num=group.boss_num,
            boss_health_ramain=group.boss_health-damage,
            challenge_damage=damage,
            is_continue=is_continue,
            comment=json.dumps(comment,
                               separators=(',', ':'),
                               ensure_ascii=False),
        )
        group.boss_health -= damage
        group.challenging_member_qq_id = None

        challenge.save()
        group.save()

        nik = user.nickname or user.qqid
        status = BossStatus(
            group.boss_cycle,
            group.boss_num,
            group.boss_health,
            0,
            f'{nik}对boss造成了{damage:,}点伤害',
        )
        self._boss_status[group_id].set_result(status)
        self._boss_status[group_id] = asyncio.Future()
        return status

    def defeat(self, group_id, qqid, behalfed=None, comment={}) -> BossStatus:
        """
        record a defeating challenge to boss

        Args:
            group_id: group id
            qqid: qqid of member who do the record
            behalfed: the real member who did the challenge
            comment: extra infomation about the challenge
        """
        group = self._group_data.get(group_id)
        if group is None:
            raise GroupError('本群未初始化')
        if behalfed is not None:
            nik = self._get_nickname_by_qqid(qqid) or qqid
            comment['behalf'] = f'由{nik}代报。'
            qqid = behalfed
        user = User.get_or_create(
            qqid=qqid,
            defaults={
                'clan_group_id': group_id,
            }
        )[0]
        d, t = pcr_datetime(area=group.game_server)
        challenges = Clan_challenge.select().where(
            Clan_challenge.gid == group_id,
            Clan_challenge.qqid == qqid,
            Clan_challenge.challenge_pcrdate == d,
        ).order_by(Clan_challenge.cid)
        challenges = list(challenges)
        if sum((not c.is_continue) for c in challenges) >= 3:
            raise InputError('今日上报次数已达到3次')
        is_continue = (challenges
                       and challenges[-1].boss_health_ramain == 0
                       and not challenges[-1].is_continue)
        challenge = Clan_challenge.create(
            gid=group_id,
            qqid=user.qqid,
            challenge_pcrdate=d,
            challenge_pcrtime=t,
            boss_cycle=group.boss_cycle,
            boss_num=group.boss_num,
            boss_health_ramain=0,
            challenge_damage=group.boss_health,
            is_continue=is_continue,
            comment=json.dumps(comment,
                               separators=(',', ':'),
                               ensure_ascii=False),
        )
        if group.boss_num == 5:
            group.boss_num = 1
            group.boss_cycle += 1
        else:
            group.boss_num += 1
        health_before = group.boss_health
        group.boss_health = (
            self.bossinfo[group.game_server]
            [self._level_by_cycle(group.boss_cycle, group.level_4)]
            [group.boss_num-1])
        group.challenging_member_qq_id = None

        group.save()
        challenge.save()
        nik = user.nickname or user.qqid
        status = BossStatus(
            group.boss_cycle,
            group.boss_num,
            group.boss_health,
            0,
            f'{nik}对boss造成了{health_before:,}点伤害，击败了boss',
        )
        self._boss_status[group_id].set_result(status)
        self._boss_status[group_id] = asyncio.Future()

        self.notify_subscribe(group_id, group.boss_num)

        return status

    def undo(self, group_id, qqid) -> BossStatus:
        """
        rollback last challenge record.

        Args:
            group_id: group id
            qqid: qqid of member who ask for the undo
        """
        group = self._group_data.get(group_id)
        if group is None:
            raise GroupError('本群未初始化')
        user = User.get_or_create(
            qqid=qqid,
            defaults={
                'clan_group_id': group_id,
            }
        )[0]
        last_challenge = self._get_previous_challenge(group_id=group_id)
        if last_challenge is None:
            raise GroupError('本群无出刀记录')
        if (last_challenge.qqid != qqid) and (user.authority_group >= 100):
            raise UserError('无权撤销')
        group.boss_cycle = last_challenge.boss_cycle
        group.boss_num = last_challenge.boss_num
        group.boss_health = (last_challenge.boss_health_ramain
                             + last_challenge.challenge_damage)
        group.challenging_member_qq_id = None
        last_challenge.delete_instance()
        group.save()

        nik = self._get_nickname_by_qqid(last_challenge.qqid)
        status = BossStatus(
            group.boss_cycle,
            group.boss_num,
            group.boss_health,
            0,
            f'{nik}的出刀记录已被撤销',
        )
        self._boss_status[group_id].set_result(status)
        self._boss_status[group_id] = asyncio.Future()
        return status

    def modify(self, group_id, cycle=None, boss_num=None, boss_health=None):
        """
        modify status of boss.

        permission should be checked before this function is called.

        Args:
            group_id: group id
            cycle: new number of clan-battle cycle
            boss_num: new number of boss
            boss_health: new value of boss health
        """
        if cycle and cycle < 1:
            raise InputError('周目数不能为负')
        if boss_num and (boss_num < 1 or boss_num > 5):
            raise InputError('boss编号必须在1~5间')
        if boss_health and boss_health < 1:
            raise InputError('boss生命值不能为负')
        group = self._group_data.get(group_id)
        if group is None:
            raise GroupError('本群未初始化')
        if cycle is not None:
            group.boss_cycle = cycle
        if boss_num is not None:
            group.boss_num = boss_num
        if boss_health is None:
            boss_health = (
                self.bossinfo[group.game_server]
                [self._level_by_cycle(group.boss_cycle, group.level_4)]
                [group.boss_num-1])
        group.boss_health = boss_health
        group.save()

        status = BossStatus(
            group.boss_cycle,
            group.boss_num,
            group.boss_health,
            0,
            'boss状态已修改',
        )
        self._boss_status[group_id].set_result(status)
        self._boss_status[group_id] = asyncio.Future()
        return status

    def change_game_server(self, group_id, game_server):
        """
        change game server.

        permission should be checked before this function is called.

        Args:
            group_id: group id
            game_server: name of game server("jp" "tw" "cn" "kr")
        """
        if game_server not in ("jp", "tw", "cn", "kr"):
            raise InputError(f'不存在{game_server}游戏服务器')
        group = self._group_data.get(group_id)
        if group is None:
            raise GroupError('本群未初始化')
        group.game_server = game_server
        group.save()

    def restart(self, group_id):
        """
        clear challenge data and reset boss status.

        challenge data should be backuped and comfirm and
        permission should be checked before this function is called.

        Args:
            group_id: group id
        """
        group = self._group_data.get(group_id)
        if group is None:
            raise GroupError('本群未初始化')
        group.boss_cycle = 1
        group.boss_num = 1
        group.boss_health = self.bossinfo[group.game_server][0][0]
        group.save()
        Clan_challenge.delete().where(
            Clan_challenge.gid == group_id,
        ).execute()

    def send_remind(self, group_id, member_list):
        """
        remind members to finish challenge

        permission should be checked before this function is called.

        Args:
            group_id: group id
            member_list: a list of qqid to reminder
        """
        message = ' '.join((
            atqq(qqid) for qqid in member_list
        ))
        asyncio.create_task(self.api.send_group_msg(
            group_id=group_id,
            message=message+'\n=======\n请及时完成今日出刀',
        ))

    def add_subscribe(self, group_id, qqid, boss_num, comment={}):
        """
        subscribe a boss, get notification when boss is defeated.

        subscribe for all boss when `boss_num` is `0`

        Args:
            group_id: group id
            qqid: qq id of subscriber
            boss_num: number of boss to subscribe, `0` for all
            comment: extra infomation about the subscribe
        """
        group = self._group_data.get(group_id)
        if group is None:
            raise GroupError('本群未初始化')
        subscribe = Clan_subscribe.get_or_none(
            gid=group_id,
            qqid=qqid,
            subscribe_item=boss_num,
        )
        if subscribe is not None:
            if boss_num == 0:
                raise UserError('您已经在树上了')
            raise UserError('您已经预约过了')
        if (boss_num == 0 and group.challenging_member_qq_id == qqid):
            # 如果挂树时当前正在挑战，则取消挑战
            group.challenging_member_qq_id = None
            group.save()
        subscribe = Clan_subscribe.create(
            gid=group_id,
            qqid=qqid,
            subscribe_item=boss_num,
            comment=json.dumps(comment,
                               separators=(',', ':'),
                               ensure_ascii=False),
        )

    def get_subscribe_list(self, group_id) -> List[Tuple[int, QQid, dict]]:
        """
        get the subscribe lists.

        return a list of subscribe infomation,
        each item is a tuple of (boss_id, qq_id, comments)

        Args:
            group_id: group id
        """
        subscribe_list = []
        for subscribe in Clan_subscribe.select().where(
            Clan_subscribe.gid == group_id,
        ):
            subscribe_list.append({
                'boss': subscribe.subscribe_item,
                'qqid': subscribe.qqid,
                'comment': json.loads(subscribe.comment),
            })
        return subscribe_list

    def cancel_subscribe(self, group_id, qqid, boss_num) -> int:
        """
        cancel a subscription.

        Args:
            group_id: group id
            qqid: qq id of subscriber
            boss_num: number of boss to be canceled
        """
        deleted_counts = Clan_subscribe.delete().where(
            Clan_subscribe.gid == group_id,
            Clan_subscribe.qqid == qqid,
            Clan_subscribe.subscribe_item == boss_num,
        ).execute()
        return deleted_counts

    def notify_subscribe(self, group_id, boss_num=None):
        """
        send notification to subsciber and remove them (when boss is defeated).

        Args:
            group_id: group id
            boss_num: number of new boss
        """
        group = self._group_data.get(group_id)
        if group is None:
            raise GroupError('本群未初始化')
        if boss_num is None:
            boss_num = group.boss_num
        notice = []
        for subscribe in Clan_subscribe.select().where(
            Clan_subscribe.gid == group_id,
            (Clan_subscribe.subscribe_item == boss_num) |
            (Clan_subscribe.subscribe_item == 0),
        ):
            notice.append(atqq(subscribe.qqid))
            subscribe.delete_instance()
        if notice:
            asyncio.create_task(self.api.send_group_msg(
                group_id=group_id,
                message='boss已被击败\n'+'\n'.join(notice),
            ))

    def apply_for_challenge(self, group_id, qqid, comment={}) -> BossStatus:
        """
        apply for a challenge to boss.

        Args:
            group_id: group id
            qqid: qq id
            comment: extra infomation about the application
        """
        group = self._group_data.get(group_id)
        if group is None:
            raise GroupError('本群未初始化')
        if group.challenging_member_qq_id is not None:
            nik = self._get_nickname_by_qqid(
                group.challenging_member_qq_id,
            ) or group.challenging_member_qq_id
            raise GroupError(f'申请失败，{nik}正在挑战boss')
        group.challenging_member_qq_id = qqid
        group.challenging_start_time = int(time.time())
        group.challenging_comment = json.dumps(comment,
                                               separators=(',', ':'),
                                               ensure_ascii=False)
        group.save()

        nik = self._get_nickname_by_qqid(qqid) or qqid
        status = BossStatus(
            group.boss_cycle,
            group.boss_num,
            group.boss_health,
            qqid,
            f'{nik}已开始boss',
        )
        self._boss_status[group_id].set_result(status)
        self._boss_status[group_id] = asyncio.Future()
        return status

    def cancel_application(self, group_id, qqid) -> BossStatus:
        """
        cancel a application of boss challenge 3 minutes after the challenge starts.

        Args:
            group_id: group id
            qqid: qq id of the canceler
            force_cancel: ignore the 3-minutes restriction
        """
        group = self._group_data.get(group_id)
        if group is None:
            raise GroupError('本群未初始化')
        if group.challenging_member_qq_id is None:
            raise GroupError('没有人正在挑战boss')
        user = User.get_or_create(
            qqid=qqid,
            defaults={
                'clan_group_id': group_id,
            }
        )[0]
        if (group.challenging_member_qq_id != qqid) and (user.authority_group >= 100):
            challenge_duration = (int(time.time())
                                  - group.challenging_start_time)
            if challenge_duration < 180:
                nik = self._get_nickname_by_qqid(
                    group.challenging_member_qq_id,
                ) or group.challenging_member_qq_id
                raise GroupError(
                    f'失败，{nik}在{challenge_duration}秒前开始挑战boss',
                )
        group.challenging_member_qq_id = None
        group.save()

        status = BossStatus(
            group.boss_cycle,
            group.boss_num,
            group.boss_health,
            0,
            'boss挑战已可申请',
        )
        self._boss_status[group_id].set_result(status)
        self._boss_status[group_id] = asyncio.Future()
        return status

    @timed_cached_func(max_len=64, max_age_seconds=60, ignore_self=True)
    def get_report(self,
                   group_id: Groupid,
                   qqid: Optional[QQid] = None,
                   pcrdate: Optional[Pcr_date] = None,
                   ) -> ClanBattleReport:
        """
        get the records

        Args:
            group_id: group id
            qqid: user id of report
            pcrdate: pcrdate of report
        """
        group = self._group_data.get(group_id)
        if group is None:
            raise GroupError('本群未初始化')
        report = []
        expressions = [
            Clan_challenge.gid == group_id,
        ]
        if qqid is not None:
            expressions.append(Clan_challenge.qqid == qqid)
        if pcrdate is not None:
            expressions.append(Clan_challenge.challenge_pcrdate == pcrdate)
        for c in Clan_challenge.select().where(
            *expressions
        ).order_by(Clan_challenge.qqid, Clan_challenge.cid):
            report.append({
                'qqid': c.qqid,
                'challenge_time': pcr_timestamp(
                    c.challenge_pcrdate,
                    c.challenge_pcrtime,
                    group.game_server,
                ),
                'cycle': c.boss_cycle,
                'boss_num': c.boss_num,
                'health_ramain': c.boss_health_ramain,
                'damage': c.challenge_damage,
                'is_continue': c.is_continue,
                'comment': json.loads(c.comment),
            })
        return report

    @timed_cached_func(max_len=16, max_age_seconds=60, ignore_self=True)
    def get_member_list(self, group_id) -> List[Dict[str, Any]]:
        """
        get the member lists from database

        return a list of member infomation,

        Args:
            group_id: group id
        """
        member_list = []
        for user in User.select().join(
            Clan_member,
            on=(User.qqid == Clan_member.qqid)
        ).where(
            Clan_member.group_id == group_id,
        ):
            member_list.append({
                'qqid': user.qqid,
                'nickname': user.nickname,
            })
        return member_list

    def jobs(self):
        trigger = CronTrigger(hour=5)

        def create_task_update_all_group_members():
            asyncio.create_task(self._update_group_list_async())

        return ((trigger, create_task_update_all_group_members),)

    def match(self, cmd):
        if not self.mode_on:
            return 0
        if len(cmd) < 2:
            return 0
        return self.Commands.get(cmd[0:2], 0)

    def execute(self, match_num, ctx):
        if ctx['message_type'] != 'group':
            if match_num < 15:
                return
        cmd = ctx['message']
        group_id = ctx['group_id']
        user_id = ctx['user_id']
        if match_num == 1:  # 创建
            match = re.match(r'^创建(?:([日台韩国])服)?公会$', cmd)
            if not match:
                return
            game_server = self.Server.get(match.group(1), 'cn')
            try:
                self.creat_group(group_id, game_server)
            except GroupError as e:
                _logger.info('群聊 失败 {} {} {}'.format(user_id, group_id, cmd))
                return str(e)
            _logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
            return ('公会创建成功，请登录后台查看，'
                    '公会战成员请发送“加入公会”，'
                    '或发送“加入全部成员”')
        elif match_num == 2:  # 加入
            if cmd == '加入公会':
                self.bind_group(group_id, user_id)
                _logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
                return '{}已加入本公会' .format(atqq(user_id))
            if cmd == '加入全部成员':
                if ctx['sender']['role'] == 'member':
                    return '只有管理员才可以这么做'
                _logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
                asyncio.create_task(
                    self._update_all_group_members_async(group_id))
                return '本群所有成员已添加记录'
        elif match_num == 3:  # 状态
            if cmd != '状态':
                return
            try:
                boss_summary = self.boss_status_summary(group_id)
            except GroupError as e:
                return str(e)
            return boss_summary
        elif match_num == 4:  # 报刀
            match = re.match(
                r'^报刀 ?(\d+)([Ww万Kk千])? *(?:\[CQ:at,qq=(\d+)\])? *$', cmd)
            if not match:
                return
            unit = {
                'W': 10000,
                'w': 10000,
                '万': 10000,
                'k': 1000,
                'K': 1000,
                '千': 1000,
            }.get(match.group(2), 1)
            damage = int(match.group(1)) * unit
            behalf = match.group(3) and int(match.group(3))
            try:
                boss_status = self.damage(group_id, user_id, damage, behalf)
            except (InputError, GroupError) as e:
                _logger.info('群聊 失败 {} {} {}'.format(user_id, group_id, cmd))
                return str(e)
            _logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
            return str(boss_status)
        elif match_num == 5:  # 尾刀
            match = re.match(
                r'^尾刀 ?(?:\[CQ:at,qq=(\d+)\])? *$', cmd)
            if not match:
                return
            behalf = match.group(1) and int(match.group(1))
            try:
                boss_status = self.defeat(group_id, user_id, behalf)
            except (InputError, GroupError) as e:
                _logger.info('群聊 失败 {} {} {}'.format(user_id, group_id, cmd))
                return str(e)
            _logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
            return str(boss_status)
        elif match_num == 6:  # 撤销
            if cmd != '撤销':
                return
            try:
                boss_status = self.undo(group_id, user_id)
            except (GroupError, UserError) as e:
                _logger.info('群聊 失败 {} {} {}'.format(user_id, group_id, cmd))
                return str(e)
            _logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
            return str(boss_status)
        elif match_num == 7:  # 修正
            if len(cmd) != 2:
                return
            url = urljoin(
                self.setting['public_address'],
                '{}clan/{}/'.format(
                    self.setting['public_basepath'],
                    group_id
                )
            )
            return '请登录面板操作：'+url
        elif match_num == 8:  # 选择
            if len(cmd) != 2:
                return
            url = urljoin(
                self.setting['public_address'],
                '{}clan/{}/setting/'.format(
                    self.setting['public_basepath'],
                    group_id
                )
            )
            return '请登录面板操作：'+url
        elif match_num == 9:  # 报告
            if len(cmd) != 2:
                return
            url = urljoin(
                self.setting['public_address'],
                '{}clan/{}/statistics/'.format(
                    self.setting['public_basepath'],
                    group_id
                )
            )
            return '请登录面板查看：'+url
        elif match_num == 10:  # 预约
            match = re.match(r'^预约([1-5])$', cmd)
            if not match:
                return
            boss_num = int(match.group(1))
            try:
                self.add_subscribe(group_id, user_id, boss_num)
            except (GroupError, UserError) as e:
                _logger.info('群聊 失败 {} {} {}'.format(user_id, group_id, cmd))
                return str(e)
            _logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
            return '预约成功'
        elif match_num == 11:  # 挂树
            if cmd != '挂树':
                return
            try:
                self.add_subscribe(group_id, user_id, 0)
            except (GroupError, UserError) as e:
                _logger.info('群聊 失败 {} {} {}'.format(user_id, group_id, cmd))
                return str(e)
            _logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
            return '已挂树'
        elif match_num == 12:  # 申请
            if cmd != '申请出刀':
                return
            try:
                boss_status = self.apply_for_challenge(group_id, user_id)
            except GroupError as e:
                _logger.info('群聊 失败 {} {} {}'.format(user_id, group_id, cmd))
                return str(e)
            _logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
            return str(boss_status)
        elif match_num == 13:  # 取消
            match = re.match(r'^取消(?:预约)?([1-5]|挂树)$', cmd)
            if not match:
                return
            b = match.group(1)
            if b == '挂树':
                boss_num = 0
                event = b
            else:
                boss_num = int(b)
                event = f'预约{b}号boss'
            counts = self.cancel_subscribe(group_id, user_id, boss_num)
            if counts == 0:
                return '你没有'+event
                _logger.info('群聊 失败 {} {} {}'.format(user_id, group_id, cmd))
            _logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
            return '已取消'+event
        elif match_num == 14:  # 解锁
            if cmd != '解锁':
                return
            try:
                boss_status = self.cancel_application(group_id, user_id)
            except GroupError as e:
                _logger.info('群聊 失败 {} {} {}'.format(user_id, group_id, cmd))
                return str(e)
            _logger.info('群聊 成功 {} {} {}'.format(user_id, group_id, cmd))
            return str(boss_status)
        elif match_num == 15:  # 面板
            if len(cmd) != 2:
                return
            url = urljoin(
                self.setting['public_address'],
                '{}clan/{}/'.format(
                    self.setting['public_basepath'],
                    group_id
                )
            )
            return '公会战面板：\n'+url

    def register_routes(self, app: Quart):

        @app.route(
            urljoin(self.setting['public_basepath'], 'clan/<int:group_id>/'),
            methods=['GET'])
        async def yobot_clan(group_id):
            if 'yobot_user' not in session:
                return redirect(url_for('yobot_login', callback=request.path))
            group = Clan_group.get_or_none(group_id=group_id)
            if group is None:
                return await render_template('404.html', item='公会'), 404
            is_member = (
                session['yobot_user']['clan_group_id'] == group.group_id)
            if (not is_member
                    and session['yobot_user']['authority_group'] >= 10):
                return await render_template('clan/unauthorized.html')
            return await render_template(
                'clan/panel.html',
                is_member=is_member,
            )

        @app.route(
            urljoin(self.setting['public_basepath'],
                    'clan/<int:group_id>/subscribers/'),
            methods=['GET'])
        async def yobot_clan_subscribers(group_id):
            if 'yobot_user' not in session:
                return redirect(url_for('yobot_login', callback=request.path))
            group = Clan_group.get_or_none(group_id=group_id)
            if group is None:
                return await render_template('404.html', item='公会'), 404
            is_member = (
                session['yobot_user']['clan_group_id'] == group.group_id)
            if (not is_member
                    and session['yobot_user']['authority_group'] >= 10):
                return await render_template('clan/unauthorized.html')
            return await render_template(
                'clan/subscribers.html',
            )

        @app.route(
            urljoin(self.setting['public_basepath'],
                    'clan/<int:group_id>/api/'),
            methods=['POST'])
        async def yobot_clan_api(group_id):
            if 'yobot_user' not in session:
                return jsonify(
                    code=10,
                    message='Not logged in',
                )
            user_id = session['yobot_user']['qqid']
            group = Clan_group.get_or_none(group_id=group_id)
            if group is None:
                return jsonify(
                    code=20,
                    message='Group not exists',
                )
            is_member = (
                session['yobot_user']['clan_group_id'] == group.group_id)
            if (not is_member
                    and session['yobot_user']['authority_group'] >= 10):
                return jsonify(
                    code=11,
                    message='Insufficient authority',
                )
            try:
                payload = await request.get_json()
                if payload is None:
                    return jsonify(
                        code=30,
                        message='Invalid payload',
                    )
                action = payload['action']
                if action == 'get_member_list':
                    return jsonify(
                        code=0,
                        members=self.get_member_list(group_id),
                    )
                elif action == 'get_data':
                    return jsonify(
                        code=0,
                        groupData={
                            'group_id': group.group_id,
                            'group_name': group.group_name,
                            'game_server': group.game_server,
                            'level_4': group.level_4,
                        },
                        bossData={
                            'cycle': group.boss_cycle,
                            'num': group.boss_num,
                            'health': group.boss_health,
                            'challenger': group.challenging_member_qq_id,
                            'full_health': (
                                self.bossinfo[group.game_server]
                                [self._level_by_cycle(
                                    group.boss_cycle, group.level_4)]
                                [group.boss_num-1]
                            ),
                        },
                        is_admin=(is_member and
                                  session['yobot_user']['authority_group'] < 100),
                        self_id=user_id,
                    )
                elif action == 'get_challenge':
                    report = self.get_report(
                        group_id,
                        None,
                        pcr_datetime(group.game_server, payload['ts'])[0],
                    )
                    return jsonify(
                        code=0,
                        challenges=report
                    )
                elif action == 'update_boss':
                    try:
                        status = await asyncio.wait_for(
                            asyncio.shield(self._boss_status[group_id]),
                            timeout=30)
                        return jsonify(
                            code=0,
                            bossData={
                                'cycle': status.cycle,
                                'num': status.num,
                                'health': status.health,
                                'challenger': status.challenger,
                                'full_health': (
                                    self.bossinfo[group.game_server]
                                    [self._level_by_cycle(
                                        status.cycle, group.level_4)]
                                    [status.num-1]
                                ),
                            },
                            notice=status.info,
                        )
                    except asyncio.TimeoutError:
                        return jsonify(
                            code=1,
                            message='not changed',
                        )
                elif action == 'addrecord':
                    if payload['defeat']:
                        try:
                            status = self.defeat(group_id,
                                                 user_id,
                                                 payload['behalf'],
                                                 )
                        except InputError as e:
                            _logger.info('网页 失败 {} {} {}'.format(
                                user_id, group_id, action))
                            return jsonify(
                                code=10,
                                message=str(e),
                            )
                        _logger.info('网页 成功 {} {} {}'.format(
                            user_id, group_id, action))
                        if group.notification & 0x01:
                            asyncio.create_task(
                                self.api.send_group_msg(
                                    group_id=group_id,
                                    message=str(status),
                                )
                            )
                        return jsonify(
                            code=0,
                            bossData={
                                'cycle': status.cycle,
                                'num': status.num,
                                'health': status.health,
                                'challenger': status.challenger,
                                'full_health': (
                                    self.bossinfo[group.game_server]
                                    [self._level_by_cycle(
                                        status.cycle, group.level_4)]
                                    [status.num-1]
                                ),
                            },
                        )
                    else:
                        try:
                            status = self.damage(group_id,
                                                 user_id,
                                                 payload['damage'],
                                                 payload['behalf'],
                                                 )
                        except InputError as e:
                            _logger.info('网页 失败 {} {} {}'.format(
                                user_id, group_id, action))
                            return jsonify(
                                code=10,
                                message=str(e),
                            )
                        _logger.info('网页 成功 {} {} {}'.format(
                            user_id, group_id, action))
                        if group.notification & 0x01:
                            asyncio.create_task(
                                self.api.send_group_msg(
                                    group_id=group_id,
                                    message=str(status),
                                )
                            )
                        return jsonify(
                            code=0,
                            bossData={
                                'cycle': status.cycle,
                                'num': status.num,
                                'health': status.health,
                                'challenger': status.challenger,
                                'full_health': (
                                    self.bossinfo[group.game_server]
                                    [self._level_by_cycle(
                                        status.cycle, group.level_4)]
                                    [status.num-1]
                                ),
                            },
                        )
                elif action == 'undo':
                    try:
                        status = self.undo(
                            group_id, user_id)
                    except (UserError, GroupError) as e:
                        _logger.info('网页 失败 {} {} {}'.format(
                            user_id, group_id, action))
                        return jsonify(
                            code=10,
                            message=str(e),
                        )
                    _logger.info('网页 成功 {} {} {}'.format(
                        user_id, group_id, action))
                    if group.notification & 0x02:
                        asyncio.create_task(
                            self.api.send_group_msg(
                                group_id=group_id,
                                message=str(status),
                            )
                        )
                    return jsonify(
                        code=0,
                        bossData={
                            'cycle': status.cycle,
                            'num': status.num,
                            'health': status.health,
                            'challenger': status.challenger,
                            'full_health': (
                                self.bossinfo[group.game_server]
                                [self._level_by_cycle(
                                    status.cycle, group.level_4)]
                                [status.num-1]
                            ),
                        },
                    )
                elif action == 'apply':
                    try:
                        status = self.apply_for_challenge(
                            group_id, user_id)
                    except GroupError as e:
                        _logger.info('网页 失败 {} {} {}'.format(
                            user_id, group_id, action))
                        return jsonify(
                            code=10,
                            message=str(e),
                        )
                    _logger.info('网页 成功 {} {} {}'.format(
                        user_id, group_id, action))
                    if group.notification & 0x04:
                        asyncio.create_task(
                            self.api.send_group_msg(
                                group_id=group_id,
                                message='{}已开始挑战boss'.format(
                                    session['yobot_user']['nickname']),
                            )
                        )
                    return jsonify(
                        code=0,
                        bossData={
                            'cycle': status.cycle,
                            'num': status.num,
                            'health': status.health,
                            'challenger': status.challenger,
                            'full_health': (
                                self.bossinfo[group.game_server]
                                [self._level_by_cycle(
                                    status.cycle, group.level_4)]
                                [status.num-1]
                            ),
                        },
                    )
                elif action == 'cancelapply':
                    try:
                        status = self.cancel_application(
                            group_id, user_id)
                    except GroupError as e:
                        _logger.info('网页 失败 {} {} {}'.format(
                            user_id, group_id, action))
                        return jsonify(
                            code=10,
                            message=str(e),
                        )
                    _logger.info('网页 成功 {} {} {}'.format(
                        user_id, group_id, action))
                    if group.notification & 0x08:
                        asyncio.create_task(
                            self.api.send_group_msg(
                                group_id=group_id,
                                message='boss挑战已可申请',
                            )
                        )
                    return jsonify(
                        code=0,
                        bossData={
                            'cycle': status.cycle,
                            'num': status.num,
                            'health': status.health,
                            'challenger': status.challenger,
                            'full_health': (
                                self.bossinfo[group.game_server]
                                [self._level_by_cycle(
                                    status.cycle, group.level_4)]
                                [status.num-1]
                            ),
                        },
                    )
                elif action == 'get_subscribers':
                    subscribers = self.get_subscribe_list(group_id)
                    return jsonify(
                        code=0,
                        group_name=group.group_name,
                        subscribers=subscribers)
                elif action == 'addsubscribe':
                    boss_num = payload['boss_num']
                    try:
                        self.add_subscribe(
                            group_id,
                            user_id,
                            boss_num,
                        )
                    except UserError as e:
                        _logger.info('网页 失败 {} {} {}'.format(
                            user_id, group_id, action))
                        return jsonify(
                            code=10,
                            message=str(e),
                        )
                    _logger.info('网页 成功 {} {} {}'.format(
                        user_id, group_id, action))
                    if boss_num == 0:
                        notice = '挂树成功'
                        if group.notification & 0x10:
                            asyncio.create_task(
                                self.api.send_group_msg(
                                    group_id=group_id,
                                    message='{}已挂树'.format(
                                        session['yobot_user']['nickname']),
                                )
                            )
                    else:
                        notice = '预约成功'
                        if group.notification & 0x40:
                            asyncio.create_task(
                                self.api.send_group_msg(
                                    group_id=group_id,
                                    message='{}已预约{}号boss'.format(
                                        session['yobot_user']['nickname'],
                                        boss_num),
                                )
                            )
                    return jsonify(code=0, notice=notice)
                elif action == 'cancelsubscribe':
                    boss_num = payload['boss_num']
                    counts = self.cancel_subscribe(
                        group_id,
                        user_id,
                        boss_num,
                    )
                    if counts == 0:
                        _logger.info('网页 失败 {} {} {}'.format(
                            user_id, group_id, action))
                        return jsonify(code=0, notice=(
                            '没有预约记录' if boss_num else '没有挂树记录'))
                    _logger.info('网页 成功 {} {} {}'.format(
                        user_id, group_id, action))
                    if boss_num == 0:
                        notice = '取消挂树成功'
                        if group.notification & 0x20:
                            asyncio.create_task(
                                self.api.send_group_msg(
                                    group_id=group_id,
                                    message='{}已取消挂树'.format(
                                        session['yobot_user']['nickname']),
                                )
                            )
                    else:
                        notice = '取消预约成功'
                        if group.notification & 0x80:
                            asyncio.create_task(
                                self.api.send_group_msg(
                                    group_id=group_id,
                                    message='{}已取消预约{}号boss'.format(
                                        session['yobot_user']['nickname'],
                                        boss_num),
                                )
                            )
                    return jsonify(code=0, notice=notice)
                elif action == 'modify':
                    if session['yobot_user']['authority_group'] >= 100:
                        return jsonify(code=11, message='Insufficient authority')
                    try:
                        status = self.modify(
                            group_id,
                            cycle=payload['cycle'],
                            boss_num=payload['boss_num'],
                            boss_health=payload['health'],
                        )
                    except InputError as e:
                        _logger.info('网页 失败 {} {} {}'.format(
                            user_id, group_id, action))
                        return jsonify(code=10, message=str(e))
                    _logger.info('网页 成功 {} {} {}'.format(
                        user_id, group_id, action))
                    if group.notification & 0x100:
                        asyncio.create_task(
                            self.api.send_group_msg(
                                group_id=group_id,
                                message=str(status),
                            )
                        )
                    return jsonify(
                        code=0,
                        bossData={
                            'cycle': status.cycle,
                            'num': status.num,
                            'health': status.health,
                            'challenger': status.challenger,
                            'full_health': (
                                self.bossinfo[group.game_server]
                                [self._level_by_cycle(
                                    status.cycle, group.level_4)]
                                [status.num-1]
                            ),
                        },
                    )
                elif action == 'send_remind':
                    if session['yobot_user']['authority_group'] >= 100:
                        return jsonify(code=11, message='Insufficient authority')
                    self.send_remind(group_id, payload['memberlist'])
                    return jsonify(
                        code=0,
                        notice='发送成功',
                    )
                elif action == 'drop_member':
                    if session['yobot_user']['authority_group'] >= 100:
                        return jsonify(code=11, message='Insufficient authority')
                    count = self.drop_member(group_id, payload['memberlist'])
                    return jsonify(
                        code=0,
                        notice=f'已删除{count}条记录',
                    )
                else:
                    return jsonify(code=32, message='unknown action')
            except KeyError as e:
                _logger.error(e)
                return jsonify(code=31, message='missing key: '+str(e))
            except Exception as e:
                _logger.exception(e)
                return jsonify(code=40, message='server error')

        @app.route(
            urljoin(self.setting['public_basepath'],
                    'clan/<int:group_id>/my/'),
            methods=['GET'])
        async def yobot_clan_user_aotu(group_id):
            if 'yobot_user' not in session:
                return redirect(url_for('yobot_login', callback=request.path))
            return redirect(url_for(
                'yobot_clan_user',
                group_id=group_id,
                qqid=session['yobot_user']['qqid'],
            ))

        @app.route(
            urljoin(self.setting['public_basepath'],
                    'clan/<int:group_id>/<int:qqid>/'),
            methods=['GET'])
        async def yobot_clan_user(group_id, qqid):
            return '建设中'

        @app.route(
            urljoin(self.setting['public_basepath'],
                    'clan/<int:group_id>/setting/'),
            methods=['GET'])
        async def yobot_clan_setting(group_id):
            if 'yobot_user' not in session:
                return redirect(url_for('yobot_login', callback=request.path))
            group = Clan_group.get_or_none(group_id=group_id)
            if group is None:
                return await render_template('404.html', item='公会'), 404
            if (session['yobot_user']['clan_group_id'] != group.group_id):
                return await render_template(
                    'unauthorized.html',
                    limit='本公会成员',
                    uath='无')
            if (session['yobot_user']['authority_group'] >= 10):
                return await render_template(
                    'unauthorized.html',
                    limit='公会战管理员',
                    uath='成员')
            return await render_template('clan/setting.html')

        @app.route(
            urljoin(self.setting['public_basepath'],
                    'clan/<int:group_id>/setting/api/'),
            methods=['POST'])
        async def yobot_clan_setting_api(group_id):
            if 'yobot_user' not in session:
                return jsonify(
                    code=10,
                    message='Not logged in',
                )
            user_id = session['yobot_user']['qqid']
            group = Clan_group.get_or_none(group_id=group_id)
            if group is None:
                return jsonify(
                    code=20,
                    message='Group not exists',
                )
            if (session['yobot_user']['clan_group_id'] != group.group_id
                    or session['yobot_user']['authority_group'] >= 10):
                return jsonify(
                    code=11,
                    message='Insufficient authority',
                )
            try:
                payload = await request.get_json()
                if payload is None:
                    return jsonify(
                        code=30,
                        message='Invalid payload',
                    )
                action = payload['action']
                if action == 'get_setting':
                    return jsonify(
                        code=0,
                        groupData={
                            'group_name': group.group_name,
                            'game_server': group.game_server,
                        },
                        notification=group.notification,
                    )
                elif action == 'put_setting':
                    group.game_server = payload['game_server']
                    group.notification = payload['notification']
                    group.save()
                    _logger.info('网页 成功 {} {} {}'.format(
                        user_id, group_id, action))
                    return jsonify(code=0, message='success')
                elif action == 'restart':
                    self.restart(group_id)
                    _logger.info('网页 成功 {} {} {}'.format(
                        user_id, group_id, action))
                    return jsonify(code=0, message='success')
                else:
                    return jsonify(code=32, message='unknown action')
            except KeyError as e:
                _logger.error(e)
                return jsonify(code=31, message='missing key: '+str(e))
            except Exception as e:
                _logger.error(e)
                return jsonify(code=40, message='server error')

        @app.route(
            urljoin(self.setting['public_basepath'],
                    'clan/<int:group_id>/statistics/'),
            methods=['GET'])
        async def yobot_clan_statistics(group_id):
            if 'yobot_user' not in session:
                return redirect(url_for('yobot_login', callback=request.path))
            group = Clan_group.get_or_none(group_id=group_id)
            if group is None:
                return await render_template('404.html', item='公会'), 404
            is_member = (
                session['yobot_user']['clan_group_id'] == group.group_id)
            if (not is_member
                    and session['yobot_user']['authority_group'] >= 10):
                return await render_template('clan/unauthorized.html')
            return await render_template(
                'clan/statistics.html',
            )

        @app.route(
            urljoin(self.setting['public_basepath'],
                    'clan/<int:group_id>/progress/'),
            methods=['GET'])
        async def yobot_clan_progress(group_id):
            if 'yobot_user' not in session:
                return redirect(url_for('yobot_login', callback=request.path))
            group = Clan_group.get_or_none(group_id=group_id)
            if group is None:
                return await render_template('404.html', item='公会'), 404
            is_member = (
                session['yobot_user']['clan_group_id'] == group.group_id)
            if (not is_member
                    and session['yobot_user']['authority_group'] >= 10):
                return await render_template('clan/unauthorized.html')
            return await render_template(
                'clan/progress.html',
            )
