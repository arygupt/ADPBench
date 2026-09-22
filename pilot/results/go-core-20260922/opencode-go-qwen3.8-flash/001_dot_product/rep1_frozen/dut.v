module dut #(
    parameter LEN = 256,
    parameter LANES = 32,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_b_flat,
    input  wire                    in_b_flat_valid,
    output wire                    in_b_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam NBEATS = LEN / LANES; // 8
    localparam NBW    = $clog2(NBEATS + 1); // 4 (covers 0..8)

    // Per-beat partial sum tree (combinational), reused each cycle.
    function [ACC_W-1:0] beat_sum(input [LANES*DATA_W-1:0] a,
                                  input [LANES*DATA_W-1:0] b);
        reg signed [ACC_W-1:0] prod [0:LANES-1];
        integer k;
        begin
            for (k = 0; k < LANES; k = k + 1)
                prod[k] = ($signed(a[k*DATA_W +: DATA_W]) *
                           $signed(b[k*DATA_W +: DATA_W]));
            beat_sum = 0;
            for (k = 0; k < LANES; k = k + 1)
                beat_sum = beat_sum + prod[k];
        end
    endfunction

    // Buffers and counts per stream.
    reg signed [ACC_W-1:0] pbuf_a [0:NBEATS-1];
    reg signed [ACC_W-1:0] pbuf_b [0:NBEATS-1];
    reg [NBW-1:0] cnt_a, cnt_b;

    // Pairing pointer and accumulation state.
    reg [NBW-1:0] i_pair;
    reg           acc_valid;
    reg signed [ACC_W-1:0] acc;
    reg           result_valid;
    reg           drain_mode;

    wire pair_done   = (i_pair == NBEATS[NBW-1:0]);
    wire can_pair    = !drain_mode && (cnt_a > i_pair) && (cnt_b > i_pair);
    wire pair_fire   = can_pair;

    assign in_a_flat_ready = (!drain_mode && !result_valid &&
                              (cnt_a < NBEATS[NBW-1:0])) ||
                             (drain_mode && (cnt_a > i_pair));
    assign in_b_flat_ready = (!drain_mode && !result_valid &&
                              (cnt_b < NBEATS[NBW-1:0])) ||
                             (drain_mode && (cnt_b > i_pair));

    wire a_accept = in_a_flat_valid && in_a_flat_ready;
    wire b_accept = in_b_flat_valid && in_b_flat_ready;
    wire a_val    = pbuf_a[i_pair];
    wire b_val    = pbuf_b[i_pair];

    assign out_valid = result_valid;
    assign out_c     = acc;
    wire out_fire    = result_valid && out_ready;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cnt_a        <= {NBW{1'b0}};
            cnt_b        <= {NBW{1'b0}};
            i_pair       <= {NBW{1'b0}};
            acc_valid    <= 1'b0;
            result_valid <= 1'b0;
            drain_mode   <= 1'b0;
            acc          <= {ACC_W{1'b0}};
        end else begin
            if (a_accept) begin
                pbuf_a[cnt_a] <= beat_sum(in_a_flat, in_b_flat);
                cnt_a         <= cnt_a + {{(NBW-1){1'b0}}, 1'b1};
            end
            if (b_accept) begin
                pbuf_b[cnt_b] <= beat_sum(in_b_flat, in_a_flat);
                cnt_b         <= cnt_b + {{(NBW-1){1'b0}}, 1'b1};
            end

            if (pair_fire) begin
                if (!acc_valid) begin
                    acc       <= a_val + b_val;
                    acc_valid <= 1'b1;
                end else begin
                    acc       <= acc + a_val + b_val;
                end
                i_pair <= i_pair + {{(NBW-1){1'b0}}, 1'b1};
                if (i_pair == NBEATS[NBW-2:0]) begin
                    result_valid <= 1'b1;
                    acc_valid    <= 1'b0;
                end
            end

            if (out_fire) begin
                result_valid <= 1'b0;
                acc_valid    <= 1'b0;
                drain_mode   <= 1'b0;
                cnt_a        <= {NBW{1'b0}};
                cnt_b        <= {NBW{1'b0}};
                i_pair       <= {NBW{1'b0}};
            end else if (pair_done) begin
                drain_mode <= 1'b1;
            end
        end
    end

endmodule
