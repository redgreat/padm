# -*- coding: utf-8 -*-

from typing import List, Dict, Optional
from aioredis import Redis

from app.api.v1.cruds.system.menu_crud import MenuCRUD
from app.api.v1.cruds.system.role_crud import RoleCRUD
from app.api.v1.schemas.system.auth_schema import AuthSchema
from app.api.v1.schemas.system.menu_schema import (
    MenuCreateSchema,
    MenuUpdateSchema,
    MenuOutSchema
)
from app.core.base_schema import BatchSetAvailable
from app.core.exceptions import CustomException
from app.core.logger import logger
from app.utils.common_util import (
    get_parent_id_map,
    get_parent_recursion,
    get_child_id_map,
    get_child_recursion
)
from app.api.v1.params.system.menu_param import MenuQueryParams
from app.api.v1.services.system.auth_service import LoginService


class MenuService:
    """
    菜单模块服务层
    """

    @classmethod
    async def get_menu_detail_service(cls, auth: AuthSchema, id: int) -> Dict:
        menu = await MenuCRUD(auth).get_by_id_crud(id=id)
        menu_dict = MenuOutSchema.model_validate(menu).model_dump()
        return menu_dict

    @classmethod
    async def get_menu_list_service(cls, auth: AuthSchema, search: MenuQueryParams, order_by: List[Dict] = None) -> List[Dict]:
        if order_by:
            order_by = eval(order_by)
        else:
            order_by = [{"order": "asc"}]
        menu_list = await MenuCRUD(auth).get_list_crud(search=search.__dict__, order_by=order_by)
        menu_dict_list = [MenuOutSchema.model_validate(menu).model_dump() for menu in menu_list]
        return menu_dict_list

    @staticmethod
    async def convert_to_menu(menu_list):
        menu_dict = {}
        result = []

        for menu in menu_list:
            if isinstance(menu, dict):
                menu_dict[menu['id']] = menu
                menu['key']=menu['id']
        for menu in menu_list:
            if isinstance(menu, dict):
                parent_id = menu.get('parent_id')
                if parent_id is None or parent_id not in menu_dict:
                    result.append(menu)
                else:
                    parent_menu = menu_dict[parent_id]
                    if 'children' not in parent_menu:
                        parent_menu['children'] = []
                    parent_menu['children'].append(menu)
        return result

    @classmethod
    async def create_menu_service(cls, auth: AuthSchema, data: MenuCreateSchema) -> Dict:
        menu = await MenuCRUD(auth).get(name=data.name)
        if menu:
            raise CustomException(msg='创建失败，该菜单已存在')

        new_menu = await MenuCRUD(auth).create(data=data)
        new_menu_dict = MenuOutSchema.model_validate(new_menu).model_dump()
        return new_menu_dict

    @classmethod
    async def update_menu_service(cls, auth: AuthSchema, data: MenuUpdateSchema, redis: Optional[Redis] = None) -> Dict:
        menu = await MenuCRUD(auth).get_by_id_crud(id=data.id)
        if not menu:
            raise CustomException(msg='更新失败，该菜单不存在')
        exist_menu = await MenuCRUD(auth).get(name=data.name)
        if exist_menu and exist_menu.id != data.id:
            raise CustomException(msg='更新失败，菜单名称重复')

        if data.parent_id:
            parent_menu = await MenuCRUD(auth).get_by_id_crud(id=data.parent_id)
            data.parent_name = parent_menu.name
        new_menu = await MenuCRUD(auth).update(id=data.id, data=data)

        await cls.set_menu_available_service(auth=auth, data=BatchSetAvailable(ids=[data.id], available=data.available))

        # 如果提供了Redis连接，刷新受影响菜单的所有用户权限
        if redis and (menu.permission != data.permission or menu.available != data.available):
            try:
                # 查找包含此菜单的所有角色
                roles_with_menu = await MenuCRUD(auth).get_roles_by_menu_id_crud(menu_id=data.id)

                # 获取所有受影响角色的ID
                role_ids = [role.id for role in roles_with_menu]

                if role_ids:
                    # 获取角色关联的所有用户
                    affected_users = await RoleCRUD(auth).get_users_by_role_ids_crud(role_ids=role_ids)

                    # 刷新每个用户的权限
                    for user in affected_users:
                        await LoginService.refresh_user_permissions_service(
                            redis=redis,
                            db=auth.db,
                            username=user.username
                        )

                    logger.info(f"已刷新菜单ID {data.id} 关联的所有用户权限")
            except Exception as e:
                logger.error(f"刷新菜单关联用户权限失败: {e}")
                # 不抛出异常，避免影响主要功能

        new_menu_dict = MenuOutSchema.model_validate(new_menu).model_dump()
        return new_menu_dict

    @classmethod
    async def delete_menu_service(cls, auth: AuthSchema, id: int) -> None:
        menu = await MenuCRUD(auth).get_by_id_crud(id=id)
        if not menu:
            raise CustomException(msg='删除失败，该菜单不存在')
        await MenuCRUD(auth).delete(ids=[id])

    @classmethod
    async def set_menu_available_service(cls, auth: AuthSchema, data: BatchSetAvailable) -> None:
        """
        递归获取所有父、子级菜单，然后批量修改菜单可用状态
        """
        menu_list = await MenuCRUD(auth).get_list_crud()
        total_ids = []

        if data.available:
            # 激活，则需要把所有父级菜单都激活
            id_map = get_parent_id_map(model_list=menu_list)
            for menu_id in data.ids:
                enable_ids = get_parent_recursion(id=menu_id, id_map=id_map)
                total_ids.extend(enable_ids)
        else:
            # 禁止，则需要把所有子级菜单都禁止
            id_map = get_child_id_map(model_list=menu_list)
            for menu_id in data.ids:
                disable_ids = get_child_recursion(id=menu_id, id_map=id_map)
                total_ids.extend(disable_ids)

        await MenuCRUD(auth).set_available_crud(ids=total_ids, available=data.available)
