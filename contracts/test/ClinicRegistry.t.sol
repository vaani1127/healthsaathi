// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {ClinicRegistry} from "../src/ClinicRegistry.sol";
import {BaseTest} from "./Base.t.sol";

contract ClinicRegistryTest is BaseTest {
    function test_constructorRejectsZeroOrDuplicateAdmins() public {
        vm.expectRevert(ClinicRegistry.InvalidAdmins.selector);
        new ClinicRegistry([admin1, address(0), admin3]);
        vm.expectRevert(ClinicRegistry.InvalidAdmins.selector);
        new ClinicRegistry([admin1, admin2, admin1]);
    }

    function test_adminsAreStored() public view {
        address[3] memory stored = registry.admins();
        assertEq(stored[0], admin1);
        assertEq(stored[1], admin2);
        assertEq(stored[2], admin3);
    }

    function test_oneConfirmationDoesNotRegister() public {
        vm.prank(admin1);
        registry.registerClinic(CLINIC, KEY_1, poster);
        assertFalse(registry.isRegistered(CLINIC));
        assertEq(registry.posterOf(CLINIC), address(0));
    }

    function test_twoAdminsRegister() public {
        vm.prank(admin1);
        registry.registerClinic(CLINIC, KEY_1, poster);

        vm.expectEmit(true, false, false, true);
        emit ClinicRegistry.ClinicRegistered(CLINIC, KEY_1, poster);
        vm.prank(admin3);
        registry.registerClinic(CLINIC, KEY_1, poster);

        assertTrue(registry.isRegistered(CLINIC));
        assertEq(registry.signerKeyHash(CLINIC), KEY_1);
        assertEq(registry.posterOf(CLINIC), poster);
        assertEq(registry.keyHistory(CLINIC).length, 1);
    }

    function test_sameAdminCannotConfirmTwice() public {
        vm.startPrank(admin1);
        registry.registerClinic(CLINIC, KEY_1, poster);
        vm.expectRevert(ClinicRegistry.AlreadyConfirmed.selector);
        registry.registerClinic(CLINIC, KEY_1, poster);
        vm.stopPrank();
        assertFalse(registry.isRegistered(CLINIC));
    }

    function test_differentArgumentsAreDifferentOperations() public {
        vm.prank(admin1);
        registry.registerClinic(CLINIC, KEY_1, poster);
        vm.prank(admin2);
        registry.registerClinic(CLINIC, KEY_2, poster);
        assertFalse(registry.isRegistered(CLINIC));
    }

    function test_cannotRegisterTwice() public {
        _register(CLINIC, KEY_1, poster);
        vm.prank(admin1);
        vm.expectRevert(ClinicRegistry.AlreadyRegistered.selector);
        registry.registerClinic(CLINIC, KEY_2, poster);
    }

    function test_rejectsZeroArguments() public {
        vm.startPrank(admin1);
        vm.expectRevert(ClinicRegistry.InvalidArgument.selector);
        registry.registerClinic(bytes32(0), KEY_1, poster);
        vm.expectRevert(ClinicRegistry.InvalidArgument.selector);
        registry.registerClinic(CLINIC, bytes32(0), poster);
        vm.expectRevert(ClinicRegistry.InvalidArgument.selector);
        registry.registerClinic(CLINIC, KEY_1, address(0));
        vm.stopPrank();
    }

    function testFuzz_nonAdminCannotRegister(address caller) public {
        vm.assume(caller != admin1 && caller != admin2 && caller != admin3);
        vm.prank(caller);
        vm.expectRevert(ClinicRegistry.NotAdmin.selector);
        registry.registerClinic(CLINIC, KEY_1, poster);
    }

    function test_rotateKeyKeepsHistory() public {
        _register(CLINIC, KEY_1, poster);

        vm.prank(admin2);
        registry.rotateKey(CLINIC, KEY_2);
        assertEq(registry.signerKeyHash(CLINIC), KEY_1);

        vm.expectEmit(true, false, false, true);
        emit ClinicRegistry.KeyRotated(CLINIC, KEY_1, KEY_2, 2);
        vm.prank(admin3);
        registry.rotateKey(CLINIC, KEY_2);

        assertEq(registry.signerKeyHash(CLINIC), KEY_2);
        bytes32[] memory history = registry.keyHistory(CLINIC);
        assertEq(history.length, 2);
        assertEq(history[0], KEY_1);
        assertEq(history[1], KEY_2);
    }

    function test_oldKeyCanBeRestored() public {
        _register(CLINIC, KEY_1, poster);
        for (uint256 i = 0; i < 2; i++) {
            bytes32 next = i == 0 ? KEY_2 : KEY_1;
            vm.prank(admin1);
            registry.rotateKey(CLINIC, next);
            vm.prank(admin2);
            registry.rotateKey(CLINIC, next);
        }
        assertEq(registry.signerKeyHash(CLINIC), KEY_1);
        assertEq(registry.keyHistory(CLINIC).length, 3);
    }

    function test_rotateKeyChecks() public {
        vm.prank(admin1);
        vm.expectRevert(ClinicRegistry.NotRegistered.selector);
        registry.rotateKey(CLINIC, KEY_2);

        _register(CLINIC, KEY_1, poster);
        vm.startPrank(admin1);
        vm.expectRevert(ClinicRegistry.InvalidArgument.selector);
        registry.rotateKey(CLINIC, KEY_1);
        vm.expectRevert(ClinicRegistry.InvalidArgument.selector);
        registry.rotateKey(CLINIC, bytes32(0));
        vm.stopPrank();

        vm.prank(makeAddr("stranger"));
        vm.expectRevert(ClinicRegistry.NotAdmin.selector);
        registry.rotateKey(CLINIC, KEY_2);
    }

    function test_lateConfirmationDoesNotRepeatAnOperation() public {
        _register(CLINIC, KEY_1, poster);
        vm.prank(admin3);
        vm.expectRevert(ClinicRegistry.AlreadyRegistered.selector);
        registry.registerClinic(CLINIC, KEY_1, poster);

        vm.prank(admin1);
        registry.rotateKey(CLINIC, KEY_2);
        vm.prank(admin2);
        registry.rotateKey(CLINIC, KEY_2);
        // The key is already KEY_2, so a third admin repeating the call is rejected.
        vm.prank(admin3);
        vm.expectRevert(ClinicRegistry.InvalidArgument.selector);
        registry.rotateKey(CLINIC, KEY_2);
    }

    function test_confirmationEvents() public {
        bytes32 operation = keccak256(abi.encode("registerClinic", CLINIC, KEY_1, poster));
        vm.expectEmit(true, true, false, true);
        emit ClinicRegistry.Confirmed(operation, admin1, 1);
        vm.prank(admin1);
        registry.registerClinic(CLINIC, KEY_1, poster);
        assertEq(registry.confirmations(operation), 1);
        assertFalse(registry.executed(operation));
    }
}
